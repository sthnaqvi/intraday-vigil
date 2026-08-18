"""`vigil paths` — the skill and any tooling resolve state-dir paths through this instead
of hardcoding a filesystem location."""
import json
import types

from vigil import config
from vigil.commands.info import cmd_paths


def test_paths_json_matches_config(capsys):
    cmd_paths(types.SimpleNamespace(json=True))
    out = json.loads(capsys.readouterr().out)
    assert out["state_dir"] == str(config.STATE_DIR)
    assert out["data_dir"] == str(config.DATA_DIR)
    assert out["status_file"] == str(config.STATUS_FILE)


def test_paths_plain_text_does_not_crash(capsys):
    cmd_paths(types.SimpleNamespace(json=False))
    out = capsys.readouterr().out
    assert "state_dir" in out
