import argparse
import json
import os
from pathlib import Path

import uvicorn

from mixxx_backend.app import create_app
from mixxx_backend.config import Settings


def write_bridge_config(settings, port):
    settings.prepare()
    config_path = settings.data_dir / "mixxx-bridge.json"
    if config_path.is_symlink():
        raise ValueError("Bridge configuration must not be a symbolic link")
    config = {
        "backend_url": f"http://127.0.0.1:{port}",
        "token_file": str(settings.data_dir / "bridge-token"),
    }
    descriptor = os.open(
        config_path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "w") as stream:
        json.dump(config, stream, indent=2)
        stream.write("\n")
    return config_path


def main():
    parser = argparse.ArgumentParser(
        description="Run the local Mixxx agent backend"
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--music-dir", type=Path, action="append")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    defaults = Settings.from_environment()
    settings = Settings(
        data_dir=(args.data_dir or defaults.data_dir).expanduser().resolve(),
        music_roots=tuple(p.expanduser().resolve() for p in args.music_dir)
        if args.music_dir is not None
        else defaults.music_roots,
    )
    config_path = write_bridge_config(settings, args.port)
    print(f"Private configuration directory: {settings.data_dir}")
    print(f"Mixxx bridge configuration: {config_path}")
    uvicorn.run(
        create_app(settings), host="127.0.0.1", port=args.port, workers=1
    )


if __name__ == "__main__":
    main()
