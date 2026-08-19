# Security

## Reporting a vulnerability

Please don't open a public issue for a security vulnerability. Open a private security
advisory on this repository's GitHub Security tab, or contact the maintainer directly if
that isn't available. Include enough detail to reproduce — what's affected, what an
attacker could do with it, and ideally a minimal repro.

## What's actually sensitive here

This is a tool that places real orders with real money against a live broker account when
configured to do so. The things worth taking seriously:

- **Broker credentials.** `KITE_API_KEY`/`KITE_API_SECRET` (`.env` in the daemon's state
  directory) and the access token (`token.json`, `chmod 600`). Compromise of either lets
  an attacker trade on the account until the token expires or the app's API key is
  revoked.
- **The dashboard.** `vigil web` binds to `127.0.0.1` only, by design, with no
  authentication layer — it relies entirely on being unreachable from outside the
  machine. Do not put it behind a reverse proxy or port-forward without adding real
  authentication in front of it; see `docs/safety.md`.
- **The Claude bridge (`claudelink.py`, `vigil ask`).** This pipes live position and P&L
  data to whatever `claude` CLI or queue mechanism is configured. That's data leaving the
  trading system into an LLM context. If you're running this against a funded account and
  care about that data boundary, review `claudelink.py` before enabling it — it's on by
  default because a `claude` binary being on `$PATH` is what triggers it, not an explicit
  opt-in today. Treat that as a known gap, not an endorsement.

## What's explicitly out of scope

Bugs in trading *strategy* (bad sector picks, wrong sizing math for your own risk
tolerance) aren't security issues — file those as regular issues. Same for a broker's own
API behaving unexpectedly; that's a broker-side concern, though a PR quarantining a new
quirk into the relevant adapter is welcome.

## Supported versions

Pre-1.0: only the latest commit on `main` is supported. There is no backport policy yet.
