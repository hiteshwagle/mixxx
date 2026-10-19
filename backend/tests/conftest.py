import copy
import wave

import pytest
from fastapi.testclient import TestClient

from mixxx_backend.app import create_app
from mixxx_backend.config import Settings
from mixxx_backend.models import CommandInput


@pytest.fixture
def rig(tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    settings = Settings(tmp_path / "data", (music,))
    app = create_app(settings, run_scheduler=False)
    with TestClient(app) as client:
        api = {"Authorization": "Bearer " + app.state.api_token}
        bridge = {"Authorization": "Bearer " + app.state.bridge_token}
        yield Rig(client, app, settings, music, api, bridge)


class Rig:
    def __init__(self, client, app, settings, music, api, bridge):
        self.client = client
        self.app = app
        self.settings = settings
        self.music = music
        self.api = api
        self.bridge = bridge
        self.instance = None
        self.state = {
            "sequence": 0,
            "crossfader": 0,
            "effects": {},
            "decks": {
                str(d): {
                    "track_id": None,
                    "playing": False,
                    "position": 0,
                    "bpm": 100,
                    "volume": 1,
                    "sync_enabled": False,
                    "eq": {"low": 1, "mid": 1, "high": 1},
                }
                for d in (1, 2)
            },
        }

    def request(self, method, path, **kwargs):
        headers = {**self.api, **kwargs.pop("headers", {})}
        return self.client.request(
            method, "/api/v1" + path, headers=headers, **kwargs
        )

    def post(self, path, data, **kwargs):
        return self.request("POST", path, json=data, **kwargs)

    def connect(self, capabilities=None):
        capabilities = capabilities or list(
            CommandInput.model_fields["command_type"].annotation.__args__
        )
        response = self.client.post(
            "/api/v1/bridge/register",
            headers=self.bridge,
            json={
                "protocol_version": 1,
                "name": "test bridge",
                "capabilities": capabilities,
            },
        )
        assert response.status_code == 201, response.text
        self.instance = response.json()["instance_id"]
        self.publish()

    def publish(self):
        self.state["sequence"] += 1
        response = self.client.post(
            "/api/v1/bridge/state",
            headers=self.bridge,
            json={
                "instance_id": self.instance,
                "state": self.state,
            },
        )
        assert response.status_code == 200, response.text
        return response

    def track(self, number=1, duration=70, metadata=True):
        path = self.music / f"track-{number}.wav"
        with wave.open(str(path), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(100)
            stream.writeframes(bytes([number % 256, 0]) * int(duration * 100))
        response = self.post("/tracks", {"path": str(path)})
        assert response.status_code == 201, response.text
        item = response.json()
        if metadata:
            response = self.request(
                "PUT",
                f"/tracks/{item['id']}/metadata",
                json={
                    "title": f"Song {number}",
                    "artist": f"Artist {number}",
                    "language": "english",
                    "explicit": False,
                    "bpm": 100,
                },
            )
            assert response.status_code == 200
            item = response.json()
        return item

    def session(self, track_ids, duration=1):
        item = self.post(
            "/dj/sessions",
            {
                "name": "test",
                "settings": {"duration_minutes": duration},
            },
        ).json()
        response = self.request(
            "PUT",
            f"/dj/sessions/{item['id']}/queue",
            json={
                "revision": 0,
                "track_ids": track_ids,
            },
        )
        assert response.status_code == 200, response.text
        return response.json()

    def command(self, data, key="test-command"):
        return self.post(
            "/dj/commands", data, headers={"Idempotency-Key": key}
        )

    def claim(self):
        return self.client.post(
            "/api/v1/bridge/commands/claim",
            headers=self.bridge,
            json={
                "instance_id": self.instance,
            },
        )

    def ack(self, identifier, success=True, state=None):
        self.state["sequence"] += 1
        return self.client.post(
            f"/api/v1/bridge/commands/{identifier}/ack",
            headers=self.bridge,
            json={
                "instance_id": self.instance,
                "success": success,
                "state": state or copy.deepcopy(self.state),
            },
        )

    def execute(self):
        response = self.claim()
        assert response.status_code == 200, response.text
        command = response.json()["command"]
        assert command is not None
        payload = command["payload"]
        operation = payload["command_type"]
        deck = self.state["decks"].get(str(payload.get("deck")))
        if operation == "load_track":
            deck.update(
                track_id=payload["track_id"], position=0, playing=False
            )
        elif operation == "set_playing":
            deck["playing"] = payload["playing"]
        elif operation == "stop":
            for d in self.state["decks"].values():
                d["playing"] = False
        elif operation == "transition":
            deck.update(playing=True, volume=1)
            self.state["decks"][str(3 - payload["deck"])]["playing"] = False
            self.state["crossfader"] = -1 if payload["deck"] == 1 else 1
        elif operation == "set_crossfader":
            self.state["crossfader"] = payload["value"]
        elif operation == "set_volume":
            deck["volume"] = payload["value"]
        else:
            raise AssertionError(operation)
        ack = self.ack(command["id"])
        assert ack.status_code == 200, ack.text
        return ack.json()
