# RCA template — post-session scoring rubric

Run after the close, once all positions are squared off. Compile the session log first,
then score.

**Audit source:** the daemon's append-only event log for today (`vigil paths --json` →
`data_dir` → `events-<YYYY-MM-DD>.jsonl`; one JSON record per line: `ts`, `type`,
`symbol`, `data`). Build the trade table from position-discovered / SL-hit /
squareoff-fill records; score lifecycle and phase-transition items (rubrics 4 and 9) from
phase-change / SL-modify-verified / SL-modify-rejected timestamps, not from memory.
Quantity-fix, SL-replaced, warning, error, kill-switch, and squareoff-start records are
discipline/hygiene evidence. Use the broker's own order history only to fill gaps from
before the daemon started (and flag that gap itself).

## Session log format

```
Date: YYYY-MM-DD
NIFTY gap: X% → [bias label]
VIX: Y → [sizing tier]
Macro theme: [selected]
Sectors selected: [A, B, C]
Sectors ranked top 3 at open: [X, Y, Z]

Trades:
| Symbol     | Dir  | Entry  | SL    | Exit   | P&L    | R-multiple | Notes          |
|------------|------|--------|-------|--------|--------|------------|----------------|
| INDIGO     | Long | ₹4,100 | ₹3,877| ₹4,300 | +₹20K  | +1.8R      |                |
| DEEPAKFERT | Long | ₹690   | ₹683  | ₹685   | -₹700  | -1.0R      | SL hit Phase 1 |

Total P&L: ₹X | Capital deployed: ₹Y | Session return: Z%
```
(Illustrative format — fill with the actual session's numbers.)

---

## 10-point scoring rubric (1–5 each, total /50)

### 1. Sector selection quality (1–5)
5 = Picked top/bottom 3 momentum sectors; macro theme alignment perfect
3 = Correct direction but missed a stronger sector available
1 = Picked sectors against momentum or against the macro theme

### 2. Entry timing vs opening range (1–5)
5 = All entries after the opening-range breakout confirmed; no premature entries
3 = Entered before the range confirmed on one trade; sector momentum confirmed
1 = Entered against the range direction, or entered at the open with no confirmation

### 3. SL placement (1–5)
5 = All SLs: ATR-appropriate, stop-hunt guard applied, clear from PDH/PDL
3 = Correct width but one SL landed near PDH/PDL (stop-hunt risk taken)
1 = SL too tight (<0.8%) causing premature stop, or too wide (>1.5%) without a stated reason

### 4. SL lifecycle execution (1–5)
5 = All phase transitions correct (breakeven at 1R, trail from 1.5R)
3 = One transition missed or delayed by one cycle
1 = SL moved in Phase 1 (untouchable violation) OR Phase 2 missed entirely

### 5. Position sizing discipline (1–5)
5 = This project's configured sizing approach applied consistently; volatility multiplier
    applied when the volatility index was elevated
3 = Sizing within 20% of correct; minor volatility adjustment missed
1 = Oversized relative to the configured approach, or volatility ignored when it was high

### 6. Macro theme alignment (1–5)
5 = All trades aligned with the chosen theme and the index gap direction
3 = 2 of 3 trades aligned; one slight mismatch
1 = Trades contradict the theme (e.g., long aviation on a crude-up day)

### 7. Reassess decision quality (1–5)
5 = Reassess done at the right time; exits/entries driven by sector re-rank data
3 = Reassess done but one decision was emotional rather than data-driven
1 = No reassess despite a major sector rank shift (>3 positions); or reassess skipped entirely

### 8. Exit execution (1–5)
5 = All MIS exited manually well before the daemon's scheduled square-off; no broker
    auto-squareoff triggered
3 = Exited most; one position left to the daemon's own square-off
1 = Multiple positions auto-squared by the broker or the daemon; significant slippage

### 9. Phase transition accuracy (1–5)
5 = Every SL modification happened within one monitor cycle of the R threshold crossing
3 = One Phase 3 trail missed for > 0.5% move without modifying
1 = Phase 2 (breakeven) not applied when profit_R crossed 1.0

### 10. Overall discipline score (1–5)
5 = Zero discretionary overrides; followed the skill's rules throughout
3 = 1–2 small deviations (documented and explained)
1 = Multiple overrides without rule basis; emotional trading decisions

---

## Output format

```
📊 POST-SESSION RCA — [Date]
═══════════════════════════════════════

SESSION LOG
[session log table from above]

SCORING
 1. Sector selection:        X/5
 2. Entry timing (ORB):      X/5
 3. SL placement:            X/5
 4. SL lifecycle:            X/5
 5. Position sizing:         X/5
 6. Macro alignment:         X/5
 7. Reassess quality:        X/5
 8. Exit execution:          X/5
 9. Phase transitions:       X/5
10. Overall discipline:      X/5
────────────────────────────────
TOTAL:                      XX/50

WHAT WENT RIGHT ✅
[2–3 specific things done well]

TOP 3 MISTAKES TO FIX TOMORROW ❌
1. [Mistake — which rule was violated — what to do instead]
2. ...
3. ...

RULE TO REMEMBER TOMORROW 📌
[One-line distillation of today's biggest lesson]
```
