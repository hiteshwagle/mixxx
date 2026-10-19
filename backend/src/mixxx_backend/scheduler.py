import asyncio
import logging
import time
from contextlib import suppress

from mixxx_backend.library import BackendError
from mixxx_backend.models import CommandInput, Rules

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, service):
        self.service = service

    def save(self, session):
        with self.service.store.transaction():
            self.service.store.put("session", session["id"], session)
            if session["status"] == "ended":
                self.service.store.event(
                    "session.completed", session["id"], session
                )

    def suspend(self, session, reason):
        session.update(status="suspended", suspension_reason=reason)
        with self.service.store.transaction():
            for command in self.service.store.all("command"):
                if command["request"]["session_id"] != session["id"]:
                    continue
                if command["status"] in {"queued", "sent"}:
                    command["status"] = (
                        "cancelled"
                        if command["status"] == "queued"
                        else "uncertain"
                    )
                    self.service.store.put("command", command["id"], command)
            self.service.store.put("session", session["id"], session)
            self.service.store.event(
                "session.suspended", session["id"], {"reason": reason}
            )

    def issue(self, session, **payload):
        runtime = session["runtime"]
        key = f"session:{session['id']}:{session['revision']}:{runtime['cursor']}:{runtime['phase']}"
        command = self.service.command(
            CommandInput(**payload), key, session["id"], automated=True
        )
        runtime["pending_command_id"] = command["id"]
        self.save(session)

    def step(self, session):
        svc = self.service
        svc.coverage(session)
        runtime = session["runtime"]
        bridge = svc.ready_bridge()
        state = bridge["state"]
        pending = runtime["pending_command_id"]
        if pending:
            command = svc.require("command", pending)
            if command["status"] in {"queued", "sent"}:
                return
            if command["status"] != "confirmed":
                raise BackendError(
                    "COMMAND_UNRESOLVED", "Session command did not complete"
                )
            phase = runtime["phase"]
            if phase == "load_first":
                runtime["phase"] = "route_first"
            elif phase == "route_first":
                runtime["phase"] = "volume_first"
            elif phase == "volume_first":
                runtime["phase"] = "play_first"
            elif phase == "play_first":
                runtime["phase"] = "preload"
                runtime["played_track_ids"].append(session["track_ids"][0])
            elif phase == "preload":
                runtime["phase"] = "wait"
            elif phase == "transition":
                runtime["cursor"] += 1
                runtime["deck"] = 3 - runtime["deck"]
                runtime["phase"] = "preload"
                runtime["played_track_ids"].append(
                    session["track_ids"][runtime["cursor"]]
                )
            elif phase == "finish":
                session["status"] = "ended"
                session["ended_at"] = time.time()
            runtime["pending_command_id"] = None
            self.save(session)
            return
        cursor = runtime["cursor"]
        deck_number = runtime["deck"]
        phase = runtime["phase"]
        if phase == "load_first":
            self.issue(
                session,
                command_type="load_track",
                deck=1,
                track_id=session["track_ids"][0],
            )
        elif phase == "route_first":
            self.issue(session, command_type="set_crossfader", value=-1)
        elif phase == "volume_first":
            self.issue(session, command_type="set_volume", deck=1, value=1)
        elif phase == "play_first":
            if state["decks"]["1"]["track_id"] != session["track_ids"][0]:
                raise BackendError(
                    "PLAYBACK_CHANGED",
                    "First deck no longer holds the requested track",
                )
            self.issue(
                session, command_type="set_playing", deck=1, playing=True
            )
        elif phase == "preload":
            current = state["decks"][str(deck_number)]
            if (
                current["track_id"] != session["track_ids"][cursor]
                or not current["playing"]
            ):
                raise BackendError(
                    "PLAYBACK_CHANGED",
                    "Active deck changed outside orchestration",
                )
            if cursor + 1 < len(session["track_ids"]):
                self.issue(
                    session,
                    command_type="load_track",
                    deck=3 - deck_number,
                    track_id=session["track_ids"][cursor + 1],
                )
            else:
                runtime["phase"] = "wait"
                self.save(session)
        elif phase == "wait":
            current = state["decks"][str(deck_number)]
            track_id = session["track_ids"][cursor]
            if current["track_id"] != track_id:
                raise BackendError(
                    "PLAYBACK_CHANGED",
                    "Loaded track changed outside orchestration",
                )
            rules = Rules.model_validate(session["settings"])
            tracks = svc.queue_tracks(session)
            elapsed = (
                sum(
                    t["duration_seconds"] - rules.transition_overlap_seconds
                    for t in tracks[:cursor]
                )
                + current["position"] * tracks[cursor]["duration_seconds"]
            )
            remaining = (1 - current["position"]) * tracks[cursor][
                "duration_seconds"
            ]
            if elapsed >= rules.duration_minutes * 60 or (
                cursor + 1 == len(tracks) and current["position"] >= 0.999
            ):
                runtime["phase"] = "finish"
                self.issue(session, command_type="stop")
            elif not current["playing"]:
                raise BackendError(
                    "PLAYBACK_PAUSED", "Playback stopped outside orchestration"
                )
            elif (
                cursor + 1 < len(tracks)
                and remaining <= rules.transition_overlap_seconds
            ):
                runtime["phase"] = "transition"
                self.issue(
                    session,
                    command_type="transition",
                    deck=3 - deck_number,
                    duration_ms=int(rules.transition_overlap_seconds * 1000),
                )

    def tick(self):
        with self.service.store.transaction():
            self.service.expire()
        for session in self.service.store.all("session"):
            if session["status"] != "running":
                continue
            try:
                self.step(session)
            except BackendError as error:
                self.suspend(session, error.code)
            except Exception:
                log.exception("Session orchestration failed")
                self.suspend(session, "INTERNAL_ERROR")

    async def run(self):
        while True:
            self.tick()
            await asyncio.sleep(0.25)


async def stop_scheduler(task):
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
