import hashlib
import math
from pathlib import Path

from mutagen import File

from mixxx_backend.models import TrackMetadata


class BackendError(Exception):
    def __init__(self, code, message, status=409):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)


class Library:
    extensions = {
        ".mp3",
        ".flac",
        ".wav",
        ".aiff",
        ".aif",
        ".m4a",
        ".ogg",
        ".opus",
    }

    def __init__(self, store, roots):
        self.store = store
        self.roots = roots

    def path(self, raw):
        path = Path(raw).expanduser().resolve()
        if not any(path.is_relative_to(root) for root in self.roots):
            raise BackendError(
                "PATH_NOT_ALLOWED",
                "File is outside configured music roots",
                403,
            )
        if not path.is_file():
            raise BackendError(
                "FILE_MISSING", "Audio file does not exist", 404
            )
        if path.suffix.lower() not in self.extensions:
            raise BackendError(
                "UNSUPPORTED_AUDIO", "Unsupported audio format", 422
            )
        return path

    def inspect(self, raw):
        path = self.path(raw)
        try:
            before = path.stat()
            media = File(path, easy=True)
            duration = float(media.info.length) if media is not None else 0
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError("Missing duration")
            tags = media.tags or {}
            metadata = TrackMetadata(
                title=str(tags.get("title", [path.stem])[0]),
                artist=str(tags.get("artist", ["Unknown artist"])[0]),
            ).model_dump()
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (
                after.st_size,
                after.st_mtime_ns,
            ):
                raise ValueError("File changed during registration")
        except Exception as error:
            raise BackendError(
                "INVALID_AUDIO", "Could not read a stable audio file", 422
            ) from error
        return {
            "id": digest,
            "path": str(path),
            "sha256": digest,
            "size": after.st_size,
            "mtime_ns": after.st_mtime_ns,
            "duration_seconds": duration,
            **metadata,
        }

    def available(self, track):
        try:
            path = self.path(track["path"])
            stat = path.stat()
            return (stat.st_size, stat.st_mtime_ns) == (
                track["size"],
                track["mtime_ns"],
            )
        except (OSError, BackendError):
            return False

    def tracks(self):
        return [
            {**track, "available": self.available(track)}
            for track in self.store.all("track")
        ]
