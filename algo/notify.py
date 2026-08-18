"""macOS desktop notifications. Best-effort — never let a notification failure touch the loop."""
from __future__ import annotations

import subprocess


def notify(message: str, title: str = "intraday-algo", sound: bool = False) -> None:
    try:
        script = f'display notification "{_esc(message)}" with title "{_esc(title)}"'
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
        if sound:
            subprocess.Popen(
                ["afplay", "/System/Library/Sounds/Sosumi.aiff"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except Exception:
        pass


def alert_dialog(message: str, title: str = "intraday-algo") -> None:
    """Modal alert for events that must not be missed (token expiry, unprotected position)."""
    try:
        script = f'display alert "{_esc(title)}" message "{_esc(message)}"'
        subprocess.Popen(
            ["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except Exception:
        pass


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')
