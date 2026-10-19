import os
import secrets
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    music_roots: tuple[Path, ...] = ()
    state_ttl: float = 5.0
    command_timeout: float = 15.0

    @classmethod
    def from_environment(cls):
        base = Path(__file__).resolve().parents[2] / ".data"
        roots = os.environ.get("MIXXX_MUSIC_ROOTS", "")
        return cls(
            data_dir=Path(os.environ.get("MIXXX_DATA_DIR", base)),
            music_roots=tuple(
                Path(p).expanduser().resolve()
                for p in roots.split(os.pathsep)
                if p
            ),
        )

    def prepare(self):
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.data_dir.chmod(0o700)

    def token(self, name: str) -> str:
        path = self.data_dir / name
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if path.is_symlink() or not path.is_file():
                raise ValueError("Token path must be a regular file")
            path.chmod(0o600)
            token = path.read_text().strip()
            if len(token) < 32:
                raise ValueError("Token file is empty or too short")
            return token
        with os.fdopen(fd, "w") as stream:
            token = secrets.token_urlsafe(32)
            stream.write(token + "\n")
        return token
