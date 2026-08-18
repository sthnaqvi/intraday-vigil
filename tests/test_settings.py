"""State-directory resolution — the fix that makes this package pip-installable.

Before this, DATA_DIR/LOGS_DIR/ENV_FILE/TOKEN_FILE were all derived from
Path(__file__).parent.parent (the source checkout). Installed into site-packages, that
would write a live broker access token, the daily event log, and armed order triggers
into the package install directory. These tests pin the replacement resolution order so a
future change can't silently reintroduce that.
"""
import importlib
import os

import pytest


@pytest.fixture
def fresh_config(monkeypatch):
    """Re-import algo.config with a clean environment so VIGIL_HOME/XDG_STATE_HOME don't
    leak in from whatever is set on the machine running the tests."""
    monkeypatch.delenv("VIGIL_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    from algo import config
    return importlib.reload(config)


def test_default_state_dir_is_dot_local_state_vigil(fresh_config):
    cfg = fresh_config
    assert cfg.STATE_DIR == (cfg.Path.home() / ".local" / "state" / "vigil")


def test_xdg_state_home_is_respected(monkeypatch, tmp_path):
    monkeypatch.delenv("VIGIL_HOME", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    from algo import config
    cfg = importlib.reload(config)
    assert cfg.STATE_DIR == (tmp_path / "vigil").resolve()


def test_vigil_home_overrides_everything(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "should-be-ignored"))
    monkeypatch.setenv("VIGIL_HOME", str(tmp_path / "explicit"))
    from algo import config
    cfg = importlib.reload(config)
    assert cfg.STATE_DIR == (tmp_path / "explicit").resolve()


def test_all_state_paths_live_under_state_dir(fresh_config):
    cfg = fresh_config
    for path in (cfg.DATA_DIR, cfg.LOGS_DIR, cfg.ENV_FILE, cfg.TOKEN_FILE,
                cfg.RISK_FILE, cfg.STATUS_FILE, cfg.PID_FILE, cfg.HOLIDAYS_FILE):
        assert cfg.STATE_DIR in path.parents or path == cfg.STATE_DIR, (
            f"{path} escaped STATE_DIR — would write outside a pip install's safe area"
        )


def test_project_root_is_never_the_base_for_state(fresh_config):
    """PROJECT_ROOT may still exist (it's used as a subprocess cwd elsewhere), but nothing
    that writes state may be derived from it — that was the entire bug."""
    cfg = fresh_config
    for path in (cfg.DATA_DIR, cfg.LOGS_DIR, cfg.ENV_FILE, cfg.TOKEN_FILE):
        assert cfg.PROJECT_ROOT not in path.parents


@pytest.fixture(autouse=True)
def _restore_config_module():
    """conftest's isolated_dirs fixture monkeypatches algo.config attributes for every
    other test in the suite; importlib.reload here would otherwise leave the module in
    whatever state the last VIGIL_HOME/XDG_STATE_HOME env var produced. Reload once more
    with a clean environment after this file's tests run so later tests see the real
    machine's default resolution again, matching every other test file's expectation.
    """
    yield
    for var in ("VIGIL_HOME", "XDG_STATE_HOME"):
        os.environ.pop(var, None)
    from algo import config
    importlib.reload(config)
