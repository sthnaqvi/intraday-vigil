# User guide: the whole system, start to finish

If you've hit "I ran the commands but nothing happened" — read this first. The short
version: **`vigil` and the Claude skill are two different things, and neither one
substitutes for the other.** Nothing in this repo opens Claude for you, and nothing in the
skill scans stocks without you being in an active conversation with Claude.

## The mental model

```
┌─────────────────────────────┐         ┌──────────────────────────────────┐
│   THE CLAUDE SKILL           │         │   VIGIL (this repo's daemon +      │
│   "the brain"                │ ──────▶ │   CLI + dashboard)                  │
│                               │  places  │   "the hands"                       │
│   Runs INSIDE a conversation  │  orders  │                                      │
│   with Claude. Decides what   │  through │   Runs as a background process.     │
│   to trade: macro read,       │  the CLI │   Owns the SL lifecycle once a      │
│   sector ranking, stock       │          │   position exists: breakeven,       │
│   scoring, position sizing,   │          │   trailing, square-off, kill        │
│   post-session review.        │          │   switch. Never decides WHAT to     │
│                               │          │   trade — only manages positions    │
│   You talk to it in English.  │          │   that already exist.               │
└─────────────────────────────┘         └──────────────────────────────────┘
```

**`vigil start` does not scan stocks, rank sectors, or place any trade.** It starts a
daemon that watches whatever positions exist (or come to exist) at the broker and manages
their stop-losses. If you run `vigil start` and then nothing happens, that's *correct* —
there's nothing for it to do yet. It's waiting for a position, the same way a smoke alarm
is "not broken" just because it hasn't gone off — it's waiting for smoke.

**Scanning stocks and deciding what to trade is the skill's job, and the skill only runs
when you're talking to Claude.** There is no button, no CLI command, and no dashboard
click that starts that process — you have to open a conversation with Claude (Claude Code,
Claude Desktop, or claude.ai — anywhere the skill is installed) and ask for it, in words,
the same way you're reading this guide right now.

## One-time setup — both halves

### 1. Install vigil

```bash
pip install "intraday-vigil[kite]"      # or intraday-vigil[paper] for no-account paper trading
```

Set up Kite credentials — see [`docs/quickstart.md`](quickstart.md) if you haven't. Verify:

```bash
vigil --help
```

### 2. Install the skill

The skill is a *separate* install, in a *separate* location, read by Claude — not by
`vigil`. From the repo root:

```bash
ln -s "$(pwd)/skill/intraday-vigil" ~/.claude/skills/intraday-vigil
```

**Verify it actually took.** This step silently does nothing useful if you already have
an *old* copy sitting at that path (a plain directory, not a symlink) — `ln -s` will
refuse to overwrite it and you'll still be running stale instructions with no error message
telling you so. Check:

```bash
readlink ~/.claude/skills/intraday-vigil
```

That must print this repo's `skill/intraday-vigil` path. If it prints nothing, the
target isn't a symlink — move the existing directory aside first
(`mv ~/.claude/skills/intraday-vigil ~/.claude/skills/intraday-vigil.bak`) and re-run
the `ln -s` above.

### 3. Confirm Claude can see it

Open a Claude conversation in an environment that loads skills from `~/.claude/skills/`
(Claude Code is one). You don't run a command to "activate" it — it's available the moment
the symlink exists and a new session starts.

## A day, start to finish — who does what

This is the actual sequence. Read the **You do** / **Claude does** / **vigil does**
columns — that's the distinction that was missing.

### Morning

**You do:** Open a conversation with Claude (this could be a Claude Code terminal session,
exactly like the one you're reading this in) and say:

> `/intraday-vigil start`

or just "start my trading session" — the skill triggers on either.

**Claude does:** Follows `skill/intraday-vigil/references/mode-start.md` step by step,
out loud, in the conversation:
1. Runs `vigil start` for you (via its own terminal access) — this is the *only* step
   that touches `vigil` directly so far.
2. Fetches the index gap and volatility level via the broker's MCP tools, computes a bias.
3. Proposes a macro theme, asks you to confirm or pick a different one.
4. Ranks all 11 sectors by live momentum, adjusted for the theme.
5. Scores 2–3 candidate stocks per top sector against a 6-point checklist.
6. Computes stop-loss price (with the stop-hunt guard) and position size, and shows you
   the numbers — symbol, qty, entry, stop, risk in currency — **and waits for you to say
   yes** before anything is placed.
7. Once you confirm, places the entry through `vigil enter` (not a raw broker call) —
   this is the second and last place `vigil` gets touched during START.

**vigil does:** From here on, silently manages the stop-loss lifecycle for whatever got
placed — breakeven at +1R, mechanical trailing at +1.5R, quantity verification every
cycle — with zero further input from you or Claude, until you ask for a status check or
the session ends.

### During the day

**You do:** Either watch the dashboard yourself (`vigil web`, then open
`http://127.0.0.1:8765`) — no Claude needed for this — or ask Claude:

> `/intraday-vigil monitor`

**Claude does:** Reads `vigil status --json` and renders it as a readable snapshot —
phases, unrealised P&L, whether anything is unprotected. It does **not** touch any order;
it only reads and reports.

**vigil does:** Keeps running its cycle in the background the entire time, regardless of
whether anyone is watching.

If momentum shifts and you want to re-rank sectors or reconsider a position:

> `/intraday-vigil reassess`

Claude re-runs the sector scan and flags anything worth reconsidering — same "propose,
then wait for your yes" pattern as START for any new entry.

### Closing the session

Two ways this ends, and both are fine:

- **Do nothing.** `vigil`'s daemon squares off everything on its own schedule (15:05 IST
  by default, ahead of the broker's own close-of-day force-square).
- **Ask Claude to do it early:** `/intraday-vigil exit` — runs `vigil squareoff` for you
  and reports the fills.

### After the close

**You do:** `/intraday-vigil rca`

**Claude does:** Reads the full event log `vigil` wrote all day (`vigil paths --json` →
`data_dir` → `events-<date>.jsonl`) and scores the session against the 10-point rubric in
`skill/intraday-vigil/references/rca-template.md` — sector selection, entry timing, SL
discipline, sizing, and more — then gives you the top 3 mistakes to fix tomorrow.

## What each `vigil` command actually does (and doesn't)

| Command | What it does | What it does NOT do |
|---|---|---|
| `vigil start` | Logs in if needed, launches the background daemon | Pick a stock, size a trade, place an order |
| `vigil web` | Serves a local dashboard | Open a browser for you — you navigate to the URL yourself |
| `vigil enter` | Places one specific trade *you already decided on* | Decide what or when to trade |
| `vigil status` | Shows the daemon's current view | Talk to Claude, or explain *why* a position looks the way it does |

If you want the "decide what to trade" part, that's always the skill, always inside a
Claude conversation — never a `vigil` command by itself.

## Troubleshooting the exact confusion this guide exists for

**"I ran `vigil start` / `vigil web` and nothing scanned any stocks."** Correct and
expected — see "The mental model" above. Open a Claude conversation and ask for
`/intraday-vigil start` if that's what you want.

**"`vigil enter` gave me an error about missing arguments."** It's not meant to be run
bare — it needs a symbol and your sizing decision:
`vigil enter RELIANCE --side long --qty 10 --sl-pct 1.0 --yes`. In normal use, Claude
fills these in for you after walking through Steps 1–6 of START; you only run it directly
if you're placing a trade you already fully decided on yourself.

**"I typed `/intraday-vigil` and nothing recognized it, or it gave weird old-looking
instructions."** The skill likely isn't actually installed, or an old copy is shadowing
the real one — see "Verify it actually took" above. `readlink
~/.claude/skills/intraday-vigil` must point into this repo.

**"The dashboard didn't open."** `vigil web` starts a *server*; it does not open a browser
tab for you. Copy the URL it prints (`http://127.0.0.1:8765` by default) into your own
browser.

## See also

- [`docs/quickstart.md`](quickstart.md) — paper-mode install and first session, command by
  command, with real output
- [`docs/usage.md`](usage.md) — every `vigil` subcommand
- [`docs/safety.md`](safety.md) — what can place a real order, and what can't
- `skill/intraday-vigil/SKILL.md` — the skill's own rules, read by Claude, not by you day
  to day — but worth reading once to know what Claude is actually going to do
