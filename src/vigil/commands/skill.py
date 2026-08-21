"""Install the Claude skill — replaces the manual `ln -s` + `readlink` dance in the
README with one command that verifies its own result, the same discipline the rest of
this codebase applies to a broker mutation: never assume a write landed, re-read it.
"""
from __future__ import annotations

import sys
from pathlib import Path

from .. import config

SKILL_NAME = "intraday-vigil"


def _skill_source() -> Path | None:
    """Where the skill's files actually live. Only resolvable from a source checkout
    today — `skill/` sits next to `src/`, not inside it, so it isn't bundled into the
    wheel (see pyproject.toml's `packages = ["src/vigil"]`). Returns None if this
    install has no skill/ directory to link to."""
    candidate = config.PROJECT_ROOT / "skill" / SKILL_NAME
    return candidate if (candidate / "SKILL.md").is_file() else None


def cmd_skill_install(args) -> int:
    source = _skill_source()
    if source is None:
        print(
            "Can't find the skill's source files next to this install — that's expected "
            "for a plain `pip install intraday-vigil` today, since skill/ isn't bundled "
            "into the package yet. Clone the repo and run this from inside it instead:\n"
            "  git clone https://github.com/sthnaqvi/intraday-vigil\n"
            "  cd intraday-vigil && vigil skill-install",
            file=sys.stderr,
        )
        return 1
    source = source.resolve()

    target = Path.home() / ".claude" / "skills" / SKILL_NAME
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.is_symlink():
        current = target.resolve()
        if current == source:
            print(f"Already installed: {target} -> {current}")
            return 0
        if not args.force:
            print(
                f"{target} already points elsewhere ({current}).\n"
                f"Re-run with --force to repoint it at {source}, "
                "or remove it yourself first.",
                file=sys.stderr,
            )
            return 1
        target.unlink()
    elif target.exists():
        print(
            f"{target} exists and is a real directory, not a symlink — refusing to "
            "touch it in case it's your own copy. Move it aside first, then re-run:\n"
            f"  mv {target} {target}.bak",
            file=sys.stderr,
        )
        return 1

    target.symlink_to(source)

    # Verify — never assume the write landed just because symlink_to() didn't raise.
    if not (target.is_symlink() and target.resolve() == source):
        print(f"Symlink created but didn't verify — check {target} by hand.",
              file=sys.stderr)
        return 1

    print(f"Installed: {target} -> {source}")
    print("Start a new Claude Code session for it to be picked up — skills load at "
          "session start, not mid-conversation.")
    return 0
