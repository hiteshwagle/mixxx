import copy
import time

import pytest
from fastapi.testclient import TestClient

from mixxx_backend.app import create_app
from mixxx_backend.config import Settings
from mixxx_backend.scheduler import Scheduler


@pytest.mark.parametrize(
    "operation,update",
    [
        ({"command_type": "set_bpm", "deck": 1, "value": 120}, {"bpm": 120}),
        ({"command_type": "seek", "deck": 1, "value": 0.5}, {"position": 0.5}),
        (
            {"command_type": "set_volume", "deck": 1, "value": 0.6},
            {"volume": 0.6},
        ),
        (
            {"command_type": "set_sync", "deck": 1, "enabled": True},
            {"sync_enabled": True},
        ),
        (
            {"command_type": "set_eq", "deck": 1, "band": "low", "value": 0.2},
            {"eq": {"low": 0.2, "mid": 1, "high": 1}},
        ),
    ],
)
def test_controls_need_matching_state(rig, operation, update):
    rig.connect()
    command = rig.command(operation).json()
    rig.claim()
    assert rig.ack(command["id"]).status_code == 409
    rig.state["decks"]["1"].update(update)
    assert rig.ack(command["id"]).json()["status"] == "confirmed"


def test_effect_and_rejected_command(rig):
    rig.connect()
    command = rig.command(
        {"command_type": "set_effect", "effect_unit": 1, "enabled": True}
    ).json()
    rig.claim()
    assert rig.ack(command["id"]).status_code == 409
    rig.state["effects"]["1"] = True
    assert rig.ack(command["id"]).json()["status"] == "confirmed"
    rejected = rig.command({"command_type": "stop"}, "stop").json()
    rig.claim()
    assert (
        rig.ack(rejected["id"], success=False).json()["status"] == "rejected"
    )


def test_ack_can_reuse_identical_recent_heartbeat(rig):
    rig.connect()
    command = rig.command({"command_type": "stop"}).json()
    rig.claim()
    rig.publish()
    body = {
        "instance_id": rig.instance,
        "state": copy.deepcopy(rig.state),
        "success": True,
    }
    assert (
        rig.client.post(
            f"/api/v1/bridge/commands/{command['id']}/ack",
            headers=rig.bridge,
            json=body,
        ).status_code
        == 200
    )


def test_queued_command_expires_without_execution(rig):
    rig.connect()
    command = rig.command({"command_type": "stop"}).json()
    with rig.app.state.service.store.transaction():
        command["deadline"] = time.time() - 1
        rig.app.state.service.store.put("command", command["id"], command)
    assert rig.claim().json()["command"] is None
    assert (
        rig.request("GET", f"/dj/commands/{command['id']}").json()["status"]
        == "expired"
    )
    assert rig.ack(command["id"]).status_code == 409


def test_old_bridge_cannot_ack_new_bridge_command(rig):
    rig.connect()
    command = rig.command({"command_type": "stop"}).json()
    rig.claim()
    instance = rig.instance
    with rig.app.state.service.store.transaction():
        bridge = rig.app.state.service.store.get("bridge", "active")
        bridge["state_received_at"] = time.time() - 60
        rig.app.state.service.store.put("bridge", "active", bridge)
    rig.connect()
    response = rig.client.post(
        f"/api/v1/bridge/commands/{command['id']}/ack",
        headers=rig.bridge,
        json={
            "instance_id": instance,
            "state": rig.state,
            "success": True,
        },
    )
    assert response.status_code == 409
    assert (
        rig.request("GET", f"/dj/commands/{command['id']}").json()["status"]
        == "uncertain"
    )


def test_disconnect_suspends_and_cancels_unsent_session_commands(rig):
    track = rig.track()
    session = rig.session([track["id"]])
    rig.connect()
    rig.post(f"/dj/sessions/{session['id']}/start", {"revision": 1})
    scheduler = Scheduler(rig.app.state.service)
    scheduler.tick()
    with rig.app.state.service.store.transaction():
        bridge = rig.app.state.service.store.get("bridge", "active")
        bridge["state_received_at"] = time.time() - 60
        rig.app.state.service.store.put("bridge", "active", bridge)
    scheduler.tick()
    result = rig.request("GET", f"/dj/sessions/{session['id']}").json()
    assert result["status"] == "suspended"
    rig.publish()
    assert rig.claim().json()["command"] is None
    assert (
        rig.request("GET", f"/dj/sessions/{session['id']}").json()["status"]
        == "suspended"
    )


def test_policy_changes_prevent_session_start(rig):
    track = rig.track()
    session = rig.session([track["id"]])
    rig.request(
        "PUT",
        f"/tracks/{track['id']}/metadata",
        json={
            "title": track["title"],
            "artist": track["artist"],
            "bpm": 100,
            "explicit": True,
        },
    )
    rig.connect()
    assert (
        rig.post(
            f"/dj/sessions/{session['id']}/start", {"revision": 1}
        ).status_code
        == 409
    )


def test_background_lifespan_shuts_down_cleanly(tmp_path):
    with TestClient(create_app(Settings(tmp_path / "data"))) as client:
        assert client.get("/api/v1/health").status_code == 200


def test_native_telemetry_preserves_values_outside_command_ranges(rig):
    rig.connect()
    rig.state["decks"]["1"].update(
        position=-0.1, bpm=350, eq={"low": 4, "mid": -2, "high": 1}
    )
    result = rig.publish().json()
    assert result["state"]["decks"]["1"]["position"] == -0.1
    assert result["state"]["decks"]["1"]["bpm"] == 350
    assert result["state"]["decks"]["1"]["eq"]["low"] == 4
    assert (
        rig.command(
            {"command_type": "seek", "deck": 1, "value": -0.1}
        ).status_code
        == 422
    )
    assert (
        rig.command(
            {"command_type": "set_bpm", "deck": 1, "value": 350}
        ).status_code
        == 422
    )
