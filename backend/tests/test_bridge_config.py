import json

import pytest

from mixxx_backend.cli import write_bridge_config
from mixxx_backend.config import Settings


def test_bridge_config_contains_paths_not_credentials(tmp_path):
    settings = Settings(tmp_path / "data")
    path = write_bridge_config(settings, 18765)
    config = json.loads(path.read_text())
    assert config == {
        "backend_url": "http://127.0.0.1:18765",
        "token_file": str(settings.data_dir / "bridge-token"),
    }
    assert path.stat().st_mode & 0o777 == 0o600
    assert not (settings.data_dir / "bridge-token").exists()


def test_bridge_config_rejects_symlink(tmp_path):
    settings = Settings(tmp_path / "data")
    settings.prepare()
    target = tmp_path / "unrelated.json"
    target.write_text("preserve")
    (settings.data_dir / "mixxx-bridge.json").symlink_to(target)
    with pytest.raises(ValueError, match="symbolic link"):
        write_bridge_config(settings, 8765)
    assert target.read_text() == "preserve"
