"""Kite Connect daily token flow.

`vigil login` opens the browser at kite's login URL; Zerodha redirects to
http://127.0.0.1:3100/kite-token-exchange?request_token=... which a one-shot local HTTP
server captures. `--paste` fallback: user pastes the full redirected URL.

One-time prerequisite: set the app's redirect URL to
http://127.0.0.1:3100/kite-token-exchange at https://developers.kite.trade.
(Port and path are config.LOGIN_LISTEN_PORT / config.LOGIN_REDIRECT_PATH — this docstring
previously said :8721/callback, which was never the real listener and would send a first-
time user's login into a dead redirect.)
"""
from __future__ import annotations

import json
import os
import threading
import webbrowser
from datetime import datetime, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from dotenv import dotenv_values

from . import clock, config

if TYPE_CHECKING:
    from kiteconnect import KiteConnect


class AuthError(Exception):
    pass


def _credentials() -> tuple[str, str]:
    env = {**dotenv_values(config.ENV_FILE), **os.environ}
    api_key = env.get("KITE_API_KEY")
    api_secret = env.get("KITE_API_SECRET")
    if not api_key or not api_secret:
        raise AuthError(f"KITE_API_KEY / KITE_API_SECRET missing — put them in {config.ENV_FILE}")
    return api_key, api_secret


def _save_token(api_key: str, access_token: str) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.TOKEN_FILE.write_text(
        json.dumps(
            {
                "api_key": api_key,
                "access_token": access_token,
                "generated_at": clock.now_ist().isoformat(),
            }
        )
    )
    os.chmod(config.TOKEN_FILE, 0o600)


def _load_token(api_key: str) -> str | None:
    if not config.TOKEN_FILE.exists():
        return None
    data = json.loads(config.TOKEN_FILE.read_text())
    if data.get("api_key") != api_key:
        return None
    generated = datetime.fromisoformat(data["generated_at"])
    now = clock.now_ist()
    # Kite tokens expire around 06:00 IST next day
    cutoff = now.replace(
        hour=config.TOKEN_VALID_FROM_HOUR_IST, minute=0, second=0, microsecond=0
    )
    if now.time() >= time(config.TOKEN_VALID_FROM_HOUR_IST, 0) and generated < cutoff:
        return None
    return data["access_token"]


class _CallbackHandler(BaseHTTPRequestHandler):
    request_token: str | None = None

    def do_GET(self):  # noqa: N802
        qs = parse_qs(urlparse(self.path).query)
        token = qs.get("request_token", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        if token:
            _CallbackHandler.request_token = token
            self.wfile.write(b"<h2>Login captured &#10003; You can close this tab.</h2>")
        else:
            self.wfile.write(b"<h2>No request_token in URL.</h2>")

    def log_message(self, *args):  # silence default request logging
        pass


def _kite_connect_cls() -> type:
    """kiteconnect is an optional dependency (`pip install vigil[kite]`) — core, and paper
    mode, must import cleanly with it absent. Only auth.py and kite_adapter.py ever import
    it, and only inside a function body, never at module level."""
    try:
        from kiteconnect import KiteConnect
    except ImportError as e:
        raise AuthError(
            "kiteconnect is not installed — run: pip install \"vigil[kite]\""
        ) from e
    return KiteConnect


def login(paste: bool = False, force: bool = False) -> str:
    """Interactive daily login. Fast-path: if today's token is still valid,
    returns it without touching the browser (unless force=True)."""
    KiteConnect = _kite_connect_cls()  # noqa: N806 — mirrors the imported class's own name
    api_key, api_secret = _credentials()
    if not force:
        token = _load_token(api_key)
        if token:
            try:
                KiteConnect(api_key=api_key, access_token=token).profile()
                print("Token already valid — no login needed.")
                return token
            except Exception:
                pass  # stale/revoked → fall through to the browser flow
    kite = KiteConnect(api_key=api_key)
    url = kite.login_url()

    if paste:
        print(f"Open this URL, log in, then paste the FULL redirected URL back here:\n\n{url}\n")
        redirected = input("Redirected URL: ").strip()
        request_token = parse_qs(urlparse(redirected).query).get("request_token", [None])[0]
        if not request_token:
            raise AuthError("Could not find request_token in the pasted URL.")
    else:
        server = HTTPServer(("127.0.0.1", config.LOGIN_LISTEN_PORT), _CallbackHandler)
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        print(f"Opening browser for Kite login "
              f"(listening on 127.0.0.1:{config.LOGIN_LISTEN_PORT})...")
        webbrowser.open(url)
        thread.join(timeout=900)
        server.server_close()
        request_token = _CallbackHandler.request_token
        if not request_token:
            raise AuthError(
                "Login not captured within 15 minutes. If the redirect URL of your Kite app "
                f"is not http://127.0.0.1:{config.LOGIN_LISTEN_PORT}{config.LOGIN_REDIRECT_PATH}, "
                "fix it at developers.kite.trade or retry with: vigil login --paste"
            )

    session = kite.generate_session(request_token, api_secret=api_secret)
    access_token = session["access_token"]
    _save_token(api_key, access_token)
    print("Login OK — token saved.")
    return access_token


def get_kite() -> KiteConnect:
    """Authenticated client from the stored token; verifies with a profile() ping."""
    KiteConnect = _kite_connect_cls()  # noqa: N806 — mirrors the imported class's own name
    api_key, _ = _credentials()
    token = _load_token(api_key)
    if not token:
        raise AuthError("No valid token for today — run: vigil login")
    kite = KiteConnect(api_key=api_key, access_token=token)
    try:
        kite.profile()
    except Exception as e:
        raise AuthError(f"Stored token rejected by Kite ({e}) — run: vigil login") from e
    return kite
