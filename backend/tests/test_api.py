import time

import pytest
from fastapi.testclient import TestClient

from mixxx_backend.app import create_app
from mixxx_backend.scheduler import Scheduler


def test_authentication_and_capabilities(rig):
    assert rig.client.get("/api/v1/health").status_code == 200
    assert rig.client.get("/api/v1/mixxx/status").status_code == 401
    assert rig.request("GET", "/mixxx/status").json()["connected"] is False
    caps = rig.request("GET", "/capabilities").json()
    assert caps["apple_music_deck_streaming"] is False
    assert caps["chatgpt_plugin_installed"] is False
    assert (
        rig.client.post(
            "/api/v1/bridge/register", headers=rig.api, json={}
        ).status_code
        == 401
    )
    assert (
        rig.client.get(
            "/api/v1/health", headers={"Host": "attacker.example"}
        ).status_code
        == 400
    )
    assert rig.client.get("/openapi.json").status_code == 404
    assert rig.request("GET", "/openapi.json").status_code == 200


def test_private_tokens_are_distinct_and_reused(rig):
    api_path = rig.settings.data_dir / "api-token"
    assert api_path.stat().st_mode & 0o777 == 0o600
    assert rig.settings.data_dir.stat().st_mode & 0o777 == 0o700
    assert rig.app.state.api_token != rig.app.state.bridge_token
    assert rig.settings.token("api-token") == rig.app.state.api_token


def test_database_rejects_second_backend(rig):
    other = create_app(rig.settings, run_scheduler=False)
    with pytest.raises(RuntimeError, match="already owns"):
        with TestClient(other):
            pass


def test_library_registration_search_and_duplicate_content(rig):
    track = rig.track()
    duplicate = rig.music / "copy.wav"
    duplicate.write_bytes(open(track["path"], "rb").read())
    assert (
        rig.post("/tracks", {"path": str(duplicate)}).json()["id"]
        == track["id"]
    )
    result = rig.request("GET", "/tracks?q=SONG").json()
    assert result["total"] == 1
    assert result["items"][0]["duration_seconds"] == 70
    assert rig.request("GET", "/tracks?q=absent").json()["total"] == 0


def test_library_boundaries_and_bad_audio(rig, tmp_path):
    outside = tmp_path / "outside.wav"
    outside.write_bytes(b"bad")
    assert rig.post("/tracks", {"path": str(outside)}).status_code == 403
    link = rig.music / "link.wav"
    link.symlink_to(outside)
    assert rig.post("/tracks", {"path": str(link)}).status_code == 403
    broken = rig.music / "bad.wav"
    broken.write_bytes(b"bad")
    assert rig.post("/tracks", {"path": str(broken)}).status_code == 422
    drm = rig.music / "protected.m4p"
    drm.write_bytes(b"bad")
    assert rig.post("/tracks", {"path": str(drm)}).status_code == 422


def test_modified_files_are_not_loadable(rig):
    track = rig.track()
    with open(track["path"], "ab") as stream:
        stream.write(b"changed")
    rig.connect()
    response = rig.command(
        {"command_type": "load_track", "deck": 1, "track_id": track["id"]}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "FILE_CHANGED"


@pytest.mark.parametrize(
    "payload",
    [
        {"command_type": "toggle_play", "deck": 1},
        {"command_type": "set_playing", "deck": 1},
        {"command_type": "stop", "deck": 1},
        {"command_type": "set_crossfader", "value": 2},
        {"command_type": "set_volume", "deck": 3, "value": 1},
        {"command_type": "seek", "deck": 1, "value": -1},
        {"command_type": "set_eq", "deck": 1, "band": "low", "value": 3},
        {"command_type": "set_bpm", "deck": 1, "value": 0},
        {"command_type": "set_effect", "effect_unit": 5, "enabled": True},
        {"command_type": "transition", "deck": 1, "duration_ms": 30001},
        {"command_type": "stop", "unknown": "field"},
    ],
)
def test_invalid_commands(rig, payload):
    assert rig.command(payload).status_code == 422


def test_offline_and_unsupported_commands(rig):
    assert rig.command({"command_type": "stop"}).status_code == 503
    rig.connect(["stop"])
    assert (
        rig.command({"command_type": "set_crossfader", "value": 0}).status_code
        == 422
    )


def test_command_delivery_is_not_confirmation_and_never_replayed(rig):
    rig.connect()
    response = rig.command({"command_type": "set_crossfader", "value": 1})
    assert response.status_code == 202
    identifier = response.json()["id"]
    assert response.json()["status"] == "queued"
    assert rig.claim().json()["command"]["id"] == identifier
    assert rig.claim().json()["command"] is None
    assert (
        rig.request("GET", f"/dj/commands/{identifier}").json()["status"]
        == "sent"
    )
    assert rig.ack(identifier).status_code == 409
    rig.state["crossfader"] = 1
    assert rig.ack(identifier).json()["status"] == "confirmed"


def test_idempotency_keys_and_missing_key(rig):
    rig.connect()
    payload = {"command_type": "stop"}
    first = rig.command(payload).json()
    assert rig.command(payload).json()["id"] == first["id"]
    assert (
        rig.command({"command_type": "set_crossfader", "value": 0}).status_code
        == 409
    )
    assert rig.post("/dj/commands", payload).status_code == 422


def test_stale_state_and_bridge_takeover(rig):
    rig.connect()
    old = rig.instance
    data = {"protocol_version": 1, "name": "other", "capabilities": ["stop"]}
    assert (
        rig.client.post(
            "/api/v1/bridge/register", headers=rig.bridge, json=data
        ).status_code
        == 409
    )
    with rig.app.state.service.store.transaction():
        bridge = rig.app.state.service.store.get("bridge", "active")
        bridge["state_received_at"] = time.time() - 60
        rig.app.state.service.store.put("bridge", "active", bridge)
    assert rig.command({"command_type": "stop"}).status_code == 503
    new = rig.client.post(
        "/api/v1/bridge/register", headers=rig.bridge, json=data
    )
    assert new.status_code == 201
    assert new.json()["instance_id"] != old
    assert rig.claim().status_code == 409


def test_state_sequences_and_ack_idempotency(rig):
    rig.connect()
    stale = rig.client.post(
        "/api/v1/bridge/state",
        headers=rig.bridge,
        json={
            "instance_id": rig.instance,
            "state": rig.state,
        },
    )
    assert stale.status_code == 409
    cmd = rig.command({"command_type": "stop"}).json()
    rig.claim()
    ack = rig.ack(cmd["id"])
    assert ack.status_code == 200
    body = {"instance_id": rig.instance, "state": rig.state, "success": True}
    repeated = rig.client.post(
        f"/api/v1/bridge/commands/{cmd['id']}/ack",
        headers=rig.bridge,
        json=body,
    )
    assert repeated.json()["status"] == "confirmed"
    body["success"] = False
    assert (
        rig.client.post(
            f"/api/v1/bridge/commands/{cmd['id']}/ack",
            headers=rig.bridge,
            json=body,
        ).status_code
        == 409
    )


def test_expired_sent_command_is_uncertain(rig):
    rig.connect()
    cmd = rig.command({"command_type": "set_crossfader", "value": 1}).json()
    rig.claim()
    with rig.app.state.service.store.transaction():
        cmd = rig.app.state.service.require("command", cmd["id"])
        cmd["deadline"] = time.time() - 1
        rig.app.state.service.store.put("command", cmd["id"], cmd)
    result = rig.request("GET", f"/dj/commands/{cmd['id']}").json()
    assert result["status"] == "uncertain"
    assert rig.claim().json()["command"] is None
    assert (
        rig.command(
            {"command_type": "set_crossfader", "value": 0}, "new"
        ).status_code
        == 409
    )
    assert (
        rig.post(
            f"/dj/commands/{cmd['id']}/reconcile", {"outcome": "confirmed"}
        ).status_code
        == 409
    )
    assert (
        rig.post(
            f"/dj/commands/{cmd['id']}/reconcile", {"outcome": "not_executed"}
        ).json()["status"]
        == "reconciled"
    )


def test_stop_cancels_unsent_commands(rig):
    rig.connect()
    cmd = rig.command({"command_type": "set_crossfader", "value": 1}).json()
    stop = rig.command({"command_type": "stop"}, "stop")
    assert stop.status_code == 202
    assert (
        rig.request("GET", f"/dj/commands/{cmd['id']}").json()["status"]
        == "cancelled"
    )
    assert rig.claim().json()["command"]["payload"]["command_type"] == "stop"


def test_state_change_before_dispatch_cancels_load(rig):
    track = rig.track()
    rig.connect()
    cmd = rig.command(
        {"command_type": "load_track", "deck": 1, "track_id": track["id"]}
    ).json()
    rig.state["decks"]["1"]["playing"] = True
    rig.publish()
    assert rig.claim().json()["command"] is None
    assert (
        rig.request("GET", f"/dj/commands/{cmd['id']}").json()["status"]
        == "cancelled"
    )


def test_deck_load_and_play_preconditions(rig):
    rig.connect()
    assert (
        rig.command(
            {"command_type": "set_playing", "deck": 1, "playing": True}
        ).status_code
        == 409
    )
    track = rig.track()
    rig.state["decks"]["1"]["playing"] = True
    rig.publish()
    assert (
        rig.command(
            {"command_type": "load_track", "deck": 1, "track_id": track["id"]}
        ).status_code
        == 409
    )


def test_full_session_executes_load_play_preload_transition_stop(rig):
    tracks = [rig.track(1, 40), rig.track(2, 40)]
    session = rig.session([t["id"] for t in tracks])
    rig.connect()
    start = rig.post(f"/dj/sessions/{session['id']}/start", {"revision": 1})
    assert start.status_code == 200, start.text
    assert start.json()["playback_started"] is False
    assert (
        rig.command({"command_type": "set_crossfader", "value": 0}).status_code
        == 409
    )
    scheduler = Scheduler(rig.app.state.service)
    for _ in range(5):
        scheduler.tick()
        rig.execute()
        scheduler.tick()
    assert rig.state["decks"]["1"]["playing"] is True
    assert rig.state["decks"]["2"]["track_id"] == tracks[1]["id"]
    rig.state["decks"]["1"]["position"] = 0.81
    rig.publish()
    scheduler.tick()
    assert rig.execute()["payload"]["command_type"] == "transition"
    scheduler.tick()
    scheduler.tick()
    rig.state["decks"]["2"]["position"] = 0.8
    rig.publish()
    scheduler.tick()
    assert rig.execute()["payload"]["command_type"] == "stop"
    scheduler.tick()
    result = rig.request("GET", f"/dj/sessions/{session['id']}").json()
    assert result["status"] == "ended"
    assert result["runtime"]["played_track_ids"] == [t["id"] for t in tracks]
    assert not any(d["playing"] for d in rig.state["decks"].values())


def test_session_queue_revision_policy_and_coverage(rig):
    track = rig.track(metadata=False)
    created = rig.post(
        "/dj/sessions", {"settings": {"duration_minutes": 1}}
    ).json()
    identifier = created["id"]
    assert (
        rig.post(
            f"/dj/sessions/{identifier}/plan", {"revision": 0}
        ).status_code
        == 409
    )
    assert (
        rig.request("GET", f"/dj/sessions/{identifier}/plan").json()[
            "complete"
        ]
        is False
    )
    clean = rig.track(2)
    result = rig.request(
        "PUT",
        f"/dj/sessions/{identifier}/queue",
        json={"revision": 0, "track_ids": [clean["id"]]},
    )
    assert result.json()["status"] == "ready"
    assert (
        rig.request(
            "PUT",
            f"/dj/sessions/{identifier}/queue",
            json={"revision": 0, "track_ids": [track["id"]]},
        ).status_code
        == 409
    )
    assert (
        rig.request(
            "PUT",
            f"/dj/sessions/{identifier}/queue",
            json={"revision": 1, "track_ids": [clean["id"], clean["id"]]},
        ).status_code
        == 409
    )


def test_start_requires_full_coverage_and_fresh_bridge(rig):
    track = rig.track(duration=30)
    session = rig.session([track["id"]])
    rig.connect()
    assert (
        rig.post(
            f"/dj/sessions/{session['id']}/start", {"revision": 1}
        ).status_code
        == 409
    )


def test_end_does_not_claim_playback_stopped(rig):
    track = rig.track()
    session = rig.session([track["id"]])
    response = rig.post(f"/dj/sessions/{session['id']}/end", {})
    assert response.json()["playback_stopped"] is False


def test_event_cursor(rig):
    rig.track()
    first = rig.request("GET", "/events?limit=1").json()["items"]
    following = rig.request(
        "GET", f"/events?after={first[0]['sequence']}"
    ).json()["items"]
    assert all(e["sequence"] > first[0]["sequence"] for e in following)


def test_restart_preserves_library_and_never_replays_commands(tmp_path):

    from mixxx_backend.config import Settings

    settings = Settings(tmp_path / "data")
    app = create_app(settings, run_scheduler=False)
    with TestClient(app):
        svc = app.state.service
        with svc.store.transaction():
            svc.store.put("command", "a", {"id": "a", "status": "sent"})
            svc.store.put("command", "b", {"id": "b", "status": "queued"})
            svc.store.put("session", "s", {"id": "s", "status": "running"})
    with TestClient(create_app(settings, run_scheduler=False)) as client:
        svc = client.app.state.service
        assert svc.require("command", "a")["status"] == "uncertain"
        assert svc.require("command", "b")["status"] == "cancelled"
        assert svc.require("session", "s")["status"] == "suspended"
        assert svc.bridge_status()["connected"] is False
