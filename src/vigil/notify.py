"""Desktop notifications, with a detected backend and NO silent failure.

Historically macOS-only (osascript/afplay), wrapped in a bare `except Exception: pass`.
Off macOS every call quietly did nothing — a Linux user running a daemon that manages
real money got zero alerts for SL hits, unprotected positions, or token expiry, with no
indication anything was wrong. That is the most dangerous portability bug this project
had: silence reads as "nothing is happening," which is the opposite of the truth for a
system watching a live position.

`can_notify()` is now checked at daemon startup (see cli.py's --allow-silent) and a live
daemon refuses to start without a working notifier, unless the user explicitly accepts
running silent.

Every notify()/alert_dialog() call also logs, unconditionally, before the OS backend is
even attempted — not as a fallback for when the backend fails, but always. The old
behaviour logged to the terminal ONLY if the OS notifier failed: on a machine with a
working notifier (the common case), a `vigil monitor` running in a terminal — the primary
way this tool is actually used — never printed what fired; only a transient OS toast (plus
a sound, if any) did. Miss the toast and you got the sound and nothing else, in the one
place you were most likely actually watching. Logging here means it lands in
logs/algo.log, which is also one of the web dashboard's Live Log sources — so this one
change makes every alert durably visible in the CLI console *and* the dashboard, with no
new UI surface required.
"""
from __future__ import annotations

import platform
import shutil
import subprocess
import sys

from .events import logger


class _Backend:
    name = "none"

    def notify(self, message: str, title: str, sound: bool) -> bool:
        raise NotImplementedError

    def alert(self, message: str, title: str) -> bool:
        raise NotImplementedError


class _MacBackend(_Backend):
    name = "macOS (osascript)"

    def notify(self, message: str, title: str, sound: bool) -> bool:
        try:
            script = f'display notification "{_esc(message)}" with title "{_esc(title)}"'
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5,
                           check=True)
        except Exception:
            return False
        if sound:
            subprocess.Popen(
                ["afplay", "/System/Library/Sounds/Sosumi.aiff"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        return True

    def alert(self, message: str, title: str) -> bool:
        try:
            script = f'display alert "{_esc(title)}" message "{_esc(message)}"'
            subprocess.Popen(["osascript", "-e", script],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            return False


class _LinuxBackend(_Backend):
    name = "Linux (notify-send)"

    def notify(self, message: str, title: str, sound: bool) -> bool:
        try:
            subprocess.run(["notify-send", title, message], capture_output=True,
                           timeout=5, check=True)
            return True
        except Exception:
            return False

    def alert(self, message: str, title: str) -> bool:
        # notify-send has no modal/blocking concept; urgency=critical is the closest
        # thing to "must not auto-dismiss" that the desktop-notification spec offers.
        try:
            subprocess.run(["notify-send", "-u", "critical", title, message],
                           capture_output=True, timeout=5, check=True)
            return True
        except Exception:
            return False


class _TerminalBackend(_Backend):
    """The universal fallback. Always succeeds, but only reaches someone actively
    watching this process's stderr — a backgrounded daemon's stderr goes to
    logs/daemon.out, which nobody watches in real time. That is exactly why this
    backend does NOT count toward can_notify(): it exists so nothing is ever fully
    silent, not to satisfy the "a live daemon needs a real notifier" requirement."""
    name = "terminal (stderr)"

    def notify(self, message: str, title: str, sound: bool) -> bool:
        bell = "\a" if sound else ""
        print(f"{bell}[{title}] {message}", file=sys.stderr, flush=True)
        return True

    def alert(self, message: str, title: str) -> bool:
        print(f"\a*** {title}: {message} ***", file=sys.stderr, flush=True)
        return True


def _detect_backend() -> _Backend | None:
    system = platform.system()
    if system == "Darwin" and shutil.which("osascript"):
        return _MacBackend()
    if system == "Linux" and shutil.which("notify-send"):
        return _LinuxBackend()
    return None


_backend = _detect_backend()
_terminal = _TerminalBackend()


def can_notify() -> bool:
    """True if a real (non-terminal) notifier is available. This is what gates whether
    a live daemon is allowed to start without --allow-silent."""
    return _backend is not None


def backend_name() -> str:
    return _backend.name if _backend else "none — terminal fallback only"


def notify(message: str, title: str = "vigil", sound: bool = False) -> None:
    logger.info("[NOTIFY] %s: %s", title, message)
    delivered = _backend.notify(message, title, sound) if _backend else False
    if not delivered:
        _terminal.notify(message, title, sound)


def alert_dialog(message: str, title: str = "vigil") -> None:
    """Modal alert for events that must not be missed (token expiry, unprotected position)."""
    logger.warning("[ALERT] %s: %s", title, message)
    delivered = _backend.alert(message, title) if _backend else False
    if not delivered:
        _terminal.alert(message, title)


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')
