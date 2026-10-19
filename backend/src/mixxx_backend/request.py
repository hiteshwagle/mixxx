import argparse
import json
import sys
import urllib.error
import urllib.request

from mixxx_backend.config import Settings


def main():
    parser = argparse.ArgumentParser(
        description="Call the local Mixxx backend"
    )
    parser.add_argument("method", choices=["GET", "POST", "PUT"])
    parser.add_argument("path", help="API path beginning /api/v1/")
    parser.add_argument("--json", default=None, help="JSON body")
    parser.add_argument("--key", help="Idempotency key for playback commands")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not args.path.startswith("/api/v1/"):
        parser.error("path must begin /api/v1/")
    try:
        token = (
            (Settings.from_environment().data_dir / "api-token")
            .read_text()
            .strip()
        )
        body = (
            json.dumps(json.loads(args.json)).encode() if args.json else None
        )
        headers = {
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        }
        if args.key:
            headers["Idempotency-Key"] = args.key
        request = urllib.request.Request(
            f"http://127.0.0.1:{args.port}{args.path}",
            data=body,
            headers=headers,
            method=args.method,
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            print(json.dumps(json.load(response), indent=2))
    except urllib.error.HTTPError as error:
        print(error.read().decode(), file=sys.stderr)
        raise SystemExit(1) from error
    except (OSError, ValueError) as error:
        print(f"Request failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
