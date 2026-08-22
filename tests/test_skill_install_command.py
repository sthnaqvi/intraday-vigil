"""`vigil skill-install` — symlinks skill/intraday-vigil/ into ~/.claude/skills/,
replacing the manual `ln -s` + `readlink` steps in the README with one verified command.
"""
import types
from pathlib import Path

import pytest

from vigil import config
from vigil.commands import skill as skill_mod
from vigil.commands.skill import SKILL_NAME, cmd_skill_install


@pytest.fixture(autouse=True)
def _fake_checkout_and_home(tmp_path, monkeypatch):
    """A fake source checkout (PROJECT_ROOT/skill/intraday-vigil/SKILL.md) and a fake
    home dir, both isolated from the real filesystem."""
    project_root = tmp_path / "checkout"
    skill_dir = project_root / "skill" / SKILL_NAME
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: intraday-vigil\n---\n")

    home = tmp_path / "home"
    home.mkdir()

    monkeypatch.setattr(config, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(Path, "home", lambda: home)
    return types.SimpleNamespace(project_root=project_root, home=home, skill_dir=skill_dir)


def _args(force=False):
    return types.SimpleNamespace(force=force)


def test_creates_symlink_pointing_at_the_skill_source(_fake_checkout_and_home, capsys):
    ctx = _fake_checkout_and_home
    rc = cmd_skill_install(_args())
    assert rc == 0

    target = ctx.home / ".claude" / "skills" / SKILL_NAME
    assert target.is_symlink()
    assert target.resolve() == ctx.skill_dir.resolve()
    assert "Installed:" in capsys.readouterr().out


def test_idempotent_when_already_correctly_installed(_fake_checkout_and_home, capsys):
    cmd_skill_install(_args())
    capsys.readouterr()  # discard first run's output

    rc = cmd_skill_install(_args())
    assert rc == 0
    assert "Already installed" in capsys.readouterr().out


def test_refuses_to_repoint_an_unrelated_symlink_without_force(_fake_checkout_and_home, capsys):
    ctx = _fake_checkout_and_home
    elsewhere = ctx.home / "elsewhere"
    elsewhere.mkdir()
    target = ctx.home / ".claude" / "skills" / SKILL_NAME
    target.parent.mkdir(parents=True)
    target.symlink_to(elsewhere)

    rc = cmd_skill_install(_args(force=False))
    assert rc == 1
    assert target.resolve() == elsewhere.resolve(), "must not touch it without --force"
    assert "--force" in capsys.readouterr().err


def test_force_repoints_an_unrelated_symlink(_fake_checkout_and_home):
    ctx = _fake_checkout_and_home
    elsewhere = ctx.home / "elsewhere"
    elsewhere.mkdir()
    target = ctx.home / ".claude" / "skills" / SKILL_NAME
    target.parent.mkdir(parents=True)
    target.symlink_to(elsewhere)

    rc = cmd_skill_install(_args(force=True))
    assert rc == 0
    assert target.resolve() == ctx.skill_dir.resolve()


def test_refuses_to_touch_a_real_directory_even_with_force(_fake_checkout_and_home, capsys):
    ctx = _fake_checkout_and_home
    target = ctx.home / ".claude" / "skills" / SKILL_NAME
    target.mkdir(parents=True)
    (target / "stale-file.md").write_text("an actual directory, not a symlink")

    rc = cmd_skill_install(_args(force=True))
    assert rc == 1
    assert not target.is_symlink()
    assert (target / "stale-file.md").exists(), "must never delete a real directory"
    assert "real directory" in capsys.readouterr().err


def test_missing_skill_source_fails_with_a_clear_message(tmp_path, monkeypatch, capsys):
    # A package root AND a PROJECT_ROOT with no skill dir at all — the "build predates
    # bundling, and it's not a source checkout either" case.
    empty_root = tmp_path / "site-packages-style-install"
    empty_root.mkdir()
    monkeypatch.setattr(config, "PROJECT_ROOT", empty_root)
    monkeypatch.setattr(skill_mod, "_package_root", lambda: empty_root)

    rc = cmd_skill_install(_args())
    assert rc == 1
    assert "pip install" in capsys.readouterr().err


def test_finds_the_skill_bundled_into_a_real_pip_install(
    _fake_checkout_and_home, tmp_path, monkeypatch, capsys
):
    """The actual point of bundling: `pip install intraday-vigil[kite]` alone — no repo
    clone, no source checkout anywhere on disk — must be enough for `vigil skill-install`
    to find something to link. PROJECT_ROOT points at an empty dir here (overriding the
    fixture's own fake checkout), proving resolution came from the bundled path, not a
    checkout fallback."""
    ctx = _fake_checkout_and_home
    site_packages_vigil = tmp_path / "site-packages" / "vigil"
    bundled_skill = site_packages_vigil / "_skill" / SKILL_NAME
    bundled_skill.mkdir(parents=True)
    (bundled_skill / "SKILL.md").write_text("---\nname: intraday-vigil\n---\n")

    no_checkout_here = tmp_path / "nothing-here"
    no_checkout_here.mkdir()
    monkeypatch.setattr(config, "PROJECT_ROOT", no_checkout_here)
    monkeypatch.setattr(skill_mod, "_package_root", lambda: site_packages_vigil)

    rc = cmd_skill_install(_args())
    assert rc == 0

    target = ctx.home / ".claude" / "skills" / SKILL_NAME
    assert target.is_symlink()
    assert target.resolve() == bundled_skill.resolve()
    assert "Installed:" in capsys.readouterr().out
