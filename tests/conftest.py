import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vigil import config  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    """Point every runtime path at a temp dir and silence desktop notifications."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "STATUS_FILE", tmp_path / "data" / "status.json")
    monkeypatch.setattr(config, "RISK_FILE", tmp_path / "data" / "risk.json")
    monkeypatch.setattr(config, "HOLIDAYS_FILE", tmp_path / "data" / "holidays.txt")

    import vigil.monitor as monitor_mod

    monkeypatch.setattr(monitor_mod, "notify", lambda *a, **k: None)
    monkeypatch.setattr(monitor_mod, "alert_dialog", lambda *a, **k: None)
    yield tmp_path
