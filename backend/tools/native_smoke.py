import argparse
import os
import socket
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path
from uuid import uuid4

import httpx


def wait_for(check, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                return value
        except (httpx.TransportError, FileNotFoundError):
            pass
        time.sleep(0.1)
    raise RuntimeError("Timed out waiting for native bridge test")


def run(binary, root, directory):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    data = directory / "data"
    music = directory / "music"
    music.mkdir()
    files = []
    for number in (1, 2):
        path = music / f"test-{number}.wav"
        with wave.open(str(path), "wb") as stream:
            stream.setnchannels(2)
            stream.setsampwidth(2)
            stream.setframerate(44100)
            stream.writeframes(bytes([number, 0, number, 0]) * 44100 * 30)
        files.append(path)
    done = directory / "done"
    backend_log = directory / "backend.log"
    native_log = directory / "native.log"
    native = None
    with (
        backend_log.open("w") as server_output,
        native_log.open("w") as native_output,
    ):
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "mixxx_backend.cli",
                "--port",
                str(port),
                "--data-dir",
                str(data),
                "--music-dir",
                str(music),
            ],
            stdout=server_output,
            stderr=subprocess.STDOUT,
        )
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", trust_env=False
            ) as api:
                wait_for(lambda: api.get("/api/v1/health").status_code == 200)
                api.headers["Authorization"] = (
                    "Bearer " + (data / "api-token").read_text().strip()
                )
                tracks = []
                for path in files:
                    response = api.post(
                        "/api/v1/tracks", json={"path": str(path)}
                    )
                    response.raise_for_status()
                    tracks.append(response.json()["id"])
                environment = {
                    **os.environ,
                    "MIXXX_AGENT_BRIDGE_TEST_CONFIG": str(
                        data / "mixxx-bridge.json"
                    ),
                    "MIXXX_AGENT_BRIDGE_TEST_DONE": str(done),
                }
                native = subprocess.Popen(
                    [
                        str(binary),
                        "--gtest_filter=PlayerManagerTest.AgentBridgeLocalBackendIntegration",
                        "--resource-path",
                        str(root / "res"),
                    ],
                    env=environment,
                    stdout=native_output,
                    stderr=subprocess.STDOUT,
                )

                def connected():
                    if native.poll() is not None:
                        raise RuntimeError(
                            "Native test process exited before connecting"
                        )
                    state = api.get("/api/v1/mixxx/status").json()
                    return state if state.get("connected") else None

                wait_for(connected)

                def command(**payload):
                    response = api.post(
                        "/api/v1/dj/commands",
                        json=payload,
                        headers={"Idempotency-Key": str(uuid4())},
                    )
                    response.raise_for_status()
                    identifier = response.json()["id"]

                    def completed():
                        result = api.get(
                            f"/api/v1/dj/commands/{identifier}"
                        ).json()
                        if result["status"] in {
                            "rejected",
                            "uncertain",
                            "expired",
                            "cancelled",
                        }:
                            raise RuntimeError(
                                f"Native command failed: {result}"
                            )
                        return (
                            result if result["status"] == "confirmed" else None
                        )

                    result = wait_for(completed, timeout=20)
                    print(f"Confirmed native {payload['command_type']}")
                    return result

                command(command_type="load_track", deck=1, track_id=tracks[0])
                command(command_type="load_track", deck=2, track_id=tracks[1])
                command(command_type="set_crossfader", value=-1)
                command(command_type="set_volume", deck=1, value=1)
                command(command_type="set_playing", deck=1, playing=True)
                command(command_type="transition", deck=2, duration_ms=250)
                command(command_type="stop")
                state = api.get("/api/v1/mixxx/status").json()
                assert state["playback_state_confirmed"]
                assert not any(
                    d["playing"] for d in state["state"]["decks"].values()
                )
                done.write_text("ok")
                if native.wait(timeout=15) != 0:
                    raise RuntimeError("Native fixture failed during shutdown")
                print(
                    "Native bridge HTTP integration passed; no audio device was opened."
                )
        except BaseException:
            print(backend_log.read_text(), file=sys.stderr)
            print(native_log.read_text(), file=sys.stderr)
            raise
        finally:
            for process in (native, server):
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()


def main():
    parser = argparse.ArgumentParser(
        description="Test the native bridge without opening an audio device"
    )
    root = Path(__file__).resolve().parents[2]
    parser.add_argument(
        "--mixxx-test", type=Path, default=root / "build" / "mixxx-test"
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(
        prefix="mixxx-native-smoke-"
    ) as directory:
        run(args.mixxx_test.resolve(), root, Path(directory))


if __name__ == "__main__":
    main()
