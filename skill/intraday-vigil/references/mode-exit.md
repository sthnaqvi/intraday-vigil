# MODE: EXIT

Square off open MIS positions manually before the daemon's own scheduled square-off. The
daemon's square-off time is deliberately set ahead of the broker's own force-square rule
(Zerodha force-squares MIS under its auction/closing rule) — see `docs/sl-rules.md` and
`src/vigil/market_profile.py` for exactly how much head start and why. If you do nothing,
the daemon flattens everything on its own at the configured time.

## All positions

```
vigil squareoff --yes
```

This is the same routine the daemon itself runs: cancel every resting SL, market-exit
every position, and wait for the broker to confirm flat before reporting. Prefer this over
driving individual MCP calls — it's atomic, gated the same way every other mutation is, and
verified rather than assumed.

## One position

```
vigil exit SYMBOL --yes
```

Cancels that symbol's SL first, then market-exits it. Confirms the fill and reports
whether the position actually went flat.

## If the daemon or CLI is unavailable

Fall back to the broker MCP directly:
1. List open MIS positions.
2. Cancel each pending SL order first.
3. Place a market exit for each, opposite side, MIS product.
4. Confirm all fills and compute total P&L.

(This is the one place the skill touches SL orders directly — cancelling them as part of a
full manual exit the user asked for. Never modify; only cancel-then-exit.)

## Exit summary

```
📋 Exit Summary — [date]
INDIGO:      +₹8,400  (+1.8R)
DEEPAKFERT:  −₹1,200  (−0.6R)
ADANIGREEN:  +₹3,100  (+1.2R)
━━━━━━━━━━━━━━━━━━━━━━━━
Net P&L:     +₹10,300
Capital used: ₹X
Return:        X.X%
```
(Illustrative format, not a real session.)
