import time

from mixxx_backend.library import BackendError, Library
from mixxx_backend.models import CommandInput, Rules
from mixxx_backend.planner import (
    blocked_reason,
    language_balance,
    plan,
    validate_queue,
)
from mixxx_backend.store import new_id


class Service:
    def __init__(self, store, settings):
        self.store = store
        self.settings = settings
        self.library = Library(store, settings.music_roots)
        with store.transaction():
            for command in store.all("command"):
                if command["status"] in {"queued", "sent"}:
                    command["status"] = (
                        "uncertain"
                        if command["status"] == "sent"
                        else "cancelled"
                    )
                    store.put("command", command["id"], command)
                    store.event("command.recovered", command["id"], command)
            for session in store.all("session"):
                if session["status"] == "running":
                    session["status"] = "suspended"
                    store.put("session", session["id"], session)
            store.put("bridge", "active", {})

    def require(self, kind, identifier):
        value = self.store.get(kind, identifier)
        if value is None:
            raise BackendError("NOT_FOUND", f"{kind} not found", 404)
        return value

    def register_track(self, inspected):
        with self.store.transaction():
            old = self.store.get("track", inspected["id"])
            if old:
                return {**old, "available": self.library.available(old)}
            self.store.put("track", inspected["id"], inspected)
            self.store.event(
                "track.registered", inspected["id"], {"id": inspected["id"]}
            )
            return {**inspected, "available": True}

    def metadata(self, identifier, metadata):
        with self.store.transaction():
            track = self.require("track", identifier)
            track.update(metadata.model_dump())
            self.store.put("track", identifier, track)
            self.store.event(
                "track.updated", identifier, metadata.model_dump()
            )
            return track

    def create_session(self, data):
        with self.store.transaction():
            session = {
                "id": new_id(),
                "name": data.name,
                "settings": data.settings.model_dump(),
                "status": "draft",
                "revision": 0,
                "track_ids": [],
                "created_at": time.time(),
            }
            self.store.put("session", session["id"], session)
            self.store.event("session.created", session["id"], session)
            return session

    def editable(self, identifier, revision):
        session = self.require("session", identifier)
        if session["revision"] != revision:
            raise BackendError(
                "STALE_REVISION", "Refresh the session before editing"
            )
        if session["status"] not in {"draft", "ready"}:
            raise BackendError(
                "SESSION_LOCKED", "Only draft or ready sessions can be edited"
            )
        return session

    def queue_tracks(self, session):
        tracks = []
        for identifier in session["track_ids"]:
            track = self.require("track", identifier)
            tracks.append(
                {**track, "available": self.library.available(track)}
            )
        return tracks

    def coverage(self, session):
        rules = Rules.model_validate(session["settings"])
        tracks = self.queue_tracks(session)
        seconds = validate_queue(tracks, rules)
        balance = language_balance(tracks, rules)
        return {
            "coverage_seconds": seconds,
            "requested_seconds": rules.duration_minutes * 60,
            "complete": seconds >= rules.duration_minutes * 60
            and balance["language_balance_ready"],
            **balance,
        }

    def update_rules(self, identifier, revision, rules):
        with self.store.transaction():
            session = self.editable(identifier, revision)
            session.update(settings=rules.model_dump(), status="draft")
            session["revision"] += 1
            self.store.put("session", identifier, session)
            self.store.event("session.rules", identifier, session)
            return session

    def preview(self, identifier):
        session = self.require("session", identifier)
        return plan(
            self.library.tracks(), Rules.model_validate(session["settings"])
        )

    def set_queue(self, identifier, revision, track_ids=None):
        with self.store.transaction():
            session = self.editable(identifier, revision)
            if track_ids is None:
                preview = self.preview(identifier)
                if not preview["complete"]:
                    raise BackendError(
                        "INSUFFICIENT_COVERAGE",
                        "Not enough policy-compliant music",
                    )
                track_ids = preview["track_ids"]
            session["track_ids"] = track_ids
            coverage = self.coverage(session)
            session["status"] = "ready" if coverage["complete"] else "draft"
            session["revision"] += 1
            self.store.put("session", identifier, session)
            self.store.event("session.queue", identifier, session)
            return {**session, "coverage": coverage}

    def expire(self):
        now = time.time()
        for command in self.store.all("command"):
            if (
                command["status"] in {"queued", "sent"}
                and now > command["deadline"]
            ):
                command["status"] = (
                    "expired" if command["status"] == "queued" else "uncertain"
                )
                self.store.put("command", command["id"], command)
                self.store.event("command.expired", command["id"], command)

    def bridge_status(self):
        bridge = self.store.get("bridge", "active") or {}
        received = bridge.get("state_received_at", 0)
        fresh = (
            bool(bridge) and time.time() - received <= self.settings.state_ttl
        )
        return {
            "connected": fresh,
            "instance_id": bridge.get("instance_id"),
            "name": bridge.get("name"),
            "capabilities": bridge.get("capabilities", []),
            "state": bridge.get("state"),
            "state_received_at": received or None,
            "state_age_seconds": max(0, time.time() - received)
            if received
            else None,
            "playback_state_confirmed": fresh,
            "audible_output_confirmed": False,
        }

    def ready_bridge(self):
        bridge = self.bridge_status()
        if not bridge["connected"]:
            raise BackendError(
                "BRIDGE_UNAVAILABLE",
                "Mixxx telemetry is missing or stale",
                503,
            )
        return bridge

    def register_bridge(self, data):
        with self.store.transaction():
            old = self.store.get("bridge", "active") or {}
            last_seen = old.get(
                "state_received_at", old.get("registered_at", 0)
            )
            if time.time() - last_seen <= self.settings.state_ttl:
                raise BackendError(
                    "BRIDGE_BUSY", "A Mixxx bridge already holds the lease"
                )
            for command in self.store.all("command"):
                if command["status"] in {"sent", "queued"}:
                    command["status"] = (
                        "uncertain"
                        if command["status"] == "sent"
                        else "cancelled"
                    )
                    self.store.put("command", command["id"], command)
            bridge = {
                **data.model_dump(),
                "instance_id": new_id(),
                "registered_at": time.time(),
            }
            self.store.put("bridge", "active", bridge)
            self.store.event(
                "bridge.registered", bridge["instance_id"], bridge
            )
            return bridge

    def bridge_instance(self, identifier):
        bridge = self.store.get("bridge", "active") or {}
        if bridge.get("instance_id") != identifier:
            raise BackendError(
                "BRIDGE_INSTANCE", "Bridge instance is no longer active"
            )
        return bridge

    def save_state(self, data):
        bridge = self.bridge_instance(data.instance_id)
        state = data.state.model_dump()
        previous = bridge.get("state", {}).get("sequence", -1)
        if state["sequence"] <= previous:
            raise BackendError("STALE_STATE", "State sequence must increase")
        for deck in state["decks"].values():
            if deck["track_id"] and not self.store.get(
                "track", deck["track_id"]
            ):
                raise BackendError(
                    "UNKNOWN_TRACK",
                    "Telemetry refers to an unregistered track",
                    422,
                )
        bridge.update(state=state, state_received_at=time.time())
        self.store.put("bridge", "active", bridge)
        return self.bridge_status()

    def state(self, data):
        with self.store.transaction():
            self.expire()
            return self.save_state(data)

    def command(
        self, data: CommandInput, key, session_id=None, automated=False
    ):
        request = {
            "input": data.model_dump(exclude_none=True),
            "session_id": session_id,
        }
        with self.store.transaction():
            self.expire()
            for old in self.store.all("command"):
                if old["idempotency_key"] == key:
                    if old["request"] != request:
                        raise BackendError(
                            "IDEMPOTENCY_CONFLICT",
                            "Key was used for a different command",
                        )
                    return old
            bridge = self.ready_bridge()
            operation = data.command_type
            active = [
                s
                for s in self.store.all("session")
                if s["status"] in {"running", "suspended"}
            ]
            if active and not automated and operation != "stop":
                raise BackendError(
                    "SESSION_OWNS_PLAYBACK",
                    "End session orchestration before manual deck control",
                )
            if operation not in bridge["capabilities"]:
                raise BackendError(
                    "UNSUPPORTED_COMMAND",
                    "Mixxx did not advertise this operation",
                    422,
                )
            pending = [
                c
                for c in self.store.all("command")
                if c["status"] in {"queued", "sent", "uncertain"}
            ]
            if pending and operation != "stop":
                raise BackendError(
                    "COMMAND_UNRESOLVED",
                    "Resolve the previous command before continuing",
                )
            payload = dict(request["input"])
            state = bridge["state"]
            deck = state["decks"].get(str(data.deck))
            if deck and operation != "load_track":
                payload["expected_track_id"] = deck["track_id"]
            if operation == "load_track":
                if deck["playing"]:
                    raise BackendError(
                        "DECK_PLAYING",
                        "Pause the destination deck before loading",
                    )
                track = self.require("track", data.track_id)
                if not self.library.available(track):
                    raise BackendError(
                        "FILE_CHANGED",
                        "Re-register missing or modified audio",
                        422,
                    )
                payload["path"] = track["path"]
            if operation in {"set_playing", "transition"} and (
                operation == "transition" or data.playing
            ):
                if not deck["track_id"]:
                    raise BackendError(
                        "DECK_EMPTY", "Load a track before playing"
                    )
            if operation == "transition":
                other = state["decks"][str(3 - data.deck)]
                if deck["playing"] or not other["playing"]:
                    raise BackendError(
                        "TRANSITION_STATE",
                        "Transition requires one playing source and a paused destination",
                    )
                payload["expected_track_id"] = deck["track_id"]
            if session_id:
                session = self.require("session", session_id)
                if session["status"] != "running":
                    raise BackendError(
                        "SESSION_NOT_RUNNING",
                        "Start session orchestration first",
                    )
                if (
                    deck
                    and deck["track_id"]
                    and operation in {"set_playing", "transition"}
                ):
                    if deck["track_id"] not in session["track_ids"]:
                        raise BackendError(
                            "TRACK_NOT_QUEUED",
                            "Loaded track is not in this session",
                        )
                check_id = (
                    data.track_id
                    if operation == "load_track"
                    else (deck["track_id"] if deck else None)
                )
                if check_id and operation in {
                    "load_track",
                    "set_playing",
                    "transition",
                }:
                    track = self.require("track", check_id)
                    reason = blocked_reason(
                        {**track, "available": self.library.available(track)},
                        Rules.model_validate(session["settings"]),
                    )
                    if reason or check_id not in session["track_ids"]:
                        raise BackendError(
                            "POLICY_BLOCKED",
                            "Track is no longer eligible for this session",
                        )
            command = {
                "id": new_id(),
                "idempotency_key": key,
                "request": request,
                "payload": payload,
                "status": "queued",
                "created_at": time.time(),
                "deadline": time.time()
                + self.settings.command_timeout
                + (data.duration_ms or 0) / 1000,
                "instance_id": bridge["instance_id"],
                "baseline_sequence": state["sequence"],
                "result": None,
            }
            if operation == "stop":
                if not automated:
                    for session in active:
                        session["status"] = "suspended"
                        session["suspension_reason"] = "STOP_REQUESTED"
                        self.store.put("session", session["id"], session)
                for old in pending:
                    if old["status"] == "queued":
                        old["status"] = "cancelled"
                        self.store.put("command", old["id"], old)
            self.store.put("command", command["id"], command)
            self.store.event("command.created", command["id"], command)
            return command

    def claim(self, instance):
        with self.store.transaction():
            self.expire()
            self.bridge_instance(instance)
            self.ready_bridge()
            queued = sorted(
                (
                    c
                    for c in self.store.all("command")
                    if c["status"] == "queued"
                ),
                key=lambda c: c["created_at"],
            )
            if not queued:
                return {"command": None}
            command = queued[0]
            session_id = command["request"]["session_id"]
            if (
                session_id
                and self.require("session", session_id)["status"] != "running"
            ):
                command["status"] = "cancelled"
                self.store.put("command", command["id"], command)
                return {"command": None}
            if command["instance_id"] != instance:
                raise BackendError(
                    "BRIDGE_INSTANCE", "Command belongs to a different bridge"
                )
            current = self.ready_bridge()["state"]
            payload = command["payload"]
            deck = current["decks"].get(str(payload.get("deck")))
            unsafe = deck and (
                (payload["command_type"] == "load_track" and deck["playing"])
                or (
                    "expected_track_id" in payload
                    and deck["track_id"] != payload["expected_track_id"]
                )
            )
            if payload["command_type"] == "load_track":
                unsafe = unsafe or not self.library.available(
                    self.require("track", payload["track_id"])
                )
            if unsafe:
                command.update(
                    status="cancelled",
                    result={"error": "STATE_CHANGED_BEFORE_DISPATCH"},
                )
                self.store.put("command", command["id"], command)
                return {"command": None}
            command["baseline_sequence"] = current["sequence"]
            command.update(status="sent", sent_at=time.time())
            self.store.put("command", command["id"], command)
            self.store.event("command.sent", command["id"], {"status": "sent"})
            return {"command": command}

    @staticmethod
    def matches(command, state):
        payload = command["payload"]
        op = payload["command_type"]
        deck = state["decks"].get(str(payload.get("deck")))
        if (
            "expected_track_id" in payload
            and deck["track_id"] != payload["expected_track_id"]
        ):
            return False

        def near(a, b):
            return abs(a - b) <= 0.001

        if op == "load_track":
            return (
                deck["track_id"] == payload["track_id"] and not deck["playing"]
            )
        if op == "set_playing":
            return deck["playing"] == payload["playing"]
        if op == "stop":
            return all(not d["playing"] for d in state["decks"].values())
        if op == "seek":
            return abs(deck["position"] - payload["value"]) <= 0.01
        if op == "set_sync":
            return deck["sync_enabled"] == payload["enabled"]
        if op == "set_crossfader":
            return near(state["crossfader"], payload["value"])
        if op == "set_eq":
            return near(deck["eq"][payload["band"]], payload["value"])
        if op == "set_effect":
            return (
                state["effects"].get(str(payload["effect_unit"]))
                == payload["enabled"]
            )
        if op == "transition":
            return (
                deck["playing"]
                and deck["track_id"] == payload["expected_track_id"]
                and near(deck["volume"], 1)
                and not state["decks"][str(3 - payload["deck"])]["playing"]
                and near(
                    state["crossfader"], -1 if payload["deck"] == 1 else 1
                )
            )
        if op in {"set_bpm", "set_volume"}:
            return near(
                deck["bpm" if op == "set_bpm" else "volume"], payload["value"]
            )
        return False

    def acknowledge(self, identifier, data):
        with self.store.transaction():
            command = self.require("command", identifier)
            if command["instance_id"] != data.instance_id:
                raise BackendError(
                    "BRIDGE_INSTANCE",
                    "Only the executing bridge can acknowledge",
                )
            self.bridge_instance(data.instance_id)
            if command["status"] in {"confirmed", "rejected"}:
                if command["ack"] != data.model_dump():
                    raise BackendError(
                        "ACK_CONFLICT",
                        "Command already has a different acknowledgement",
                    )
                return command
            if command["status"] not in {"sent", "uncertain"}:
                raise BackendError(
                    "COMMAND_NOT_SENT", "Command was not dispatched"
                )
            snapshot = data.state.model_dump()
            if snapshot["sequence"] <= command["baseline_sequence"]:
                raise BackendError(
                    "STALE_STATE", "Acknowledgement must follow dispatch"
                )
            if data.success and not self.matches(command, snapshot):
                raise BackendError(
                    "STATE_MISMATCH",
                    "Observed state does not confirm requested result",
                )
            bridge = self.bridge_instance(data.instance_id)
            previous = bridge.get("state", {})
            if (
                snapshot["sequence"] == previous.get("sequence")
                and snapshot == previous
            ):
                pass
            else:
                self.save_state(data)
            command.update(
                status="confirmed" if data.success else "rejected",
                result={"state": snapshot, "error": data.error},
                ack=data.model_dump(),
                completed_at=time.time(),
            )
            self.store.put("command", identifier, command)
            self.store.event("command.acknowledged", identifier, command)
            return command

    def reconcile(self, identifier, outcome):
        with self.store.transaction():
            self.expire()
            command = self.require("command", identifier)
            if command["status"] != "uncertain":
                raise BackendError(
                    "NOT_UNCERTAIN",
                    "Only uncertain commands can be reconciled",
                )
            state = self.ready_bridge()["state"]
            if outcome == "confirmed" and not self.matches(command, state):
                raise BackendError(
                    "STATE_MISMATCH", "Current state does not match command"
                )
            command.update(
                status="reconciled",
                result={
                    "operator_outcome": outcome,
                    "state": state,
                    "evidence": "operator_reconciliation",
                },
            )
            self.store.put("command", identifier, command)
            self.store.event("command.reconciled", identifier, command)
            return command

    def session_status(self, identifier):
        session = self.require("session", identifier)
        return {**session, "playback": self.bridge_status()}

    def start_session(self, identifier, revision):
        with self.store.transaction():
            session = self.editable(identifier, revision)
            if not self.coverage(session)["complete"]:
                raise BackendError(
                    "INSUFFICIENT_COVERAGE",
                    "Queue does not cover the requested runtime",
                )
            if any(
                s["status"] in {"running", "suspended"}
                for s in self.store.all("session")
            ):
                raise BackendError(
                    "SESSION_ACTIVE", "End the existing active session first"
                )
            bridge = self.ready_bridge()
            if any(d["playing"] for d in bridge["state"]["decks"].values()):
                raise BackendError(
                    "PLAYBACK_ACTIVE",
                    "Pause decks before starting a new session",
                )
            if any(
                c["status"] in {"queued", "sent", "uncertain"}
                for c in self.store.all("command")
            ):
                raise BackendError(
                    "COMMAND_UNRESOLVED",
                    "Resolve outstanding playback commands first",
                )
            session.update(status="running", started_at=time.time())
            session["runtime"] = {
                "cursor": 0,
                "deck": 1,
                "phase": "load_first",
                "pending_command_id": None,
                "played_track_ids": [],
            }
            needed = {
                "load_track",
                "set_playing",
                "stop",
                "set_volume",
                "set_crossfader",
            }
            if len(session["track_ids"]) > 1:
                needed.add("transition")
            if not needed.issubset(bridge["capabilities"]):
                raise BackendError(
                    "UNSUPPORTED_COMMAND",
                    "Bridge lacks session playback capabilities",
                    422,
                )
            session["revision"] += 1
            self.store.put("session", identifier, session)
            self.store.event("session.started", identifier, session)
            return {**session, "playback_started": False}

    def end_session(self, identifier):
        with self.store.transaction():
            session = self.require("session", identifier)
            for command in self.store.all("command"):
                if (
                    command["request"]["session_id"] == identifier
                    and command["status"] == "queued"
                ):
                    command["status"] = "cancelled"
                    self.store.put("command", command["id"], command)
            session.update(status="ended", ended_at=time.time())
            session["revision"] += 1
            self.store.put("session", identifier, session)
            self.store.event("session.ended", identifier, session)
            return {**session, "playback_stopped": False}

    def upcoming(self, identifier):
        session = self.require("session", identifier)
        return {
            "revision": session["revision"],
            "tracks": self.queue_tracks(session),
            "basis": "planned_queue; not playback history",
        }
