# Incidents: reference data drift

Distinct from `verification-gaps.md` — those are code paths that didn't check their own
work; this is external data (broker instrument identifiers) that changed after the skill's
reference file was written, with nothing checking it against a live source since.

## A hand-maintained token list went stale, and one entry went stale silently

A skill reference file carries exchange instrument identifiers for a few dozen stocks,
used to fetch historical data directly. It was rewritten wholesale during an unrelated
migration/rewrite pass without validating any of the values against a live source. A full
audit later found roughly a quarter of the identifiers wrong, plus a few symbols that had
been renamed or delisted since the file was last touched.

Most of the wrong identifiers failed loudly — a lookup for the wrong instrument simply
returned no data, which is annoying but obvious. One did not: it returned a complete,
plausible-looking data series for a *different* instrument entirely, at a completely
different price scale. Nothing about the response looked wrong on its own. It was only
caught by chance, cross-referencing against a live quote for an unrelated reason — had it
been used directly for scoring, it would have silently fed a wrong trend read into a real
trading decision.

**Fix:** a validation script now cross-checks every identifier in the reference file
against the broker's live instrument master and can rewrite mismatches automatically. It's
wired into the session-start workflow to run once per session, before the reference data is
used for anything — the whole point being that "checked once, a year ago" is not the same
guarantee as "checked today," and exchange identifiers are exactly the kind of value that
looks static but isn't.

**The deeper lesson:** any hand-maintained file referencing an external system's identifiers
needs a live cross-check on some cadence, not a one-time population. A silent wrong answer
is more dangerous than a loud one — the loud failures here (no data returned) were merely
inconvenient; the one silent failure was the one that could have actually cost money.
