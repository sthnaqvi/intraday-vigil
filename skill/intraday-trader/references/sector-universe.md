# Sector Universe — 11 Sectors for Dynamic Ranking

Use these tokens for batch `get_quotes()` calls. Compute % from open per stock, average per sector, rank all 11.

## Banking
- NSE:HDFCBANK   (token 341249)
- NSE:ICICIBANK  (token 1270529)
- NSE:SBIN       (token 779521)
- NSE:KOTAKBANK  (token 492033)

## IT / Technology
- NSE:INFY       (token 408065)
- NSE:TCS        (token 2953217)
- NSE:WIPRO      (token 969473)
- NSE:HCLTECH    (token 1207553)

## Auto
- NSE:MARUTI     (token 2815745)
- NSE:TATAMOTORS (token 884737)
- NSE:BAJAJ-AUTO (token 4268801)
- NSE:EICHERMOT  (token 232961)

## Pharma
- NSE:SUNPHARMA  (token 857857)
- NSE:DRREDDY    (token 225537)
- NSE:CIPLA      (token 177921)

## FMCG
- NSE:HINDUNILVR (token 356865)
- NSE:NESTLEIND  (token 4598529)
- NSE:ITC        (token 424961)

## Metal / Mining
- NSE:TATASTEEL  (token 895745)
- NSE:JSWSTEEL   (token 3001089)
- NSE:HINDALCO   (token 348929)

## Energy / Oil & Gas
- NSE:RELIANCE   (token 738561)
- NSE:ONGC       (token 633601)
- NSE:BPCL       (token 134657)

## Realty
- NSE:DLF        (token 3771073)
- NSE:GODREJPROP (token 3401473)
- NSE:OBEROIRLTY (token 1906945)

## Aviation
- NSE:INDIGO     (token 2865921)  ← InterGlobe Aviation
- NSE:SPICEJET   (token 807553)

## Chemical Fertiliser
- NSE:DEEPAKFERT (token 211713)
- NSE:COROMANDEL (token 203265)
- NSE:CHAMBAL    (token 163073)

## Renewable Energy
- NSE:ADANIGREEN (token 912129)
- NSE:TATAPOWER  (token 877473)
- NSE:TORNTPOWER (token 539937)

---

## Computation Logic

```
# 1. Batch fetch all stocks
quotes = mcp__kite__get_quotes(["NSE:HDFCBANK", "NSE:ICICIBANK", ..., all symbols])

# 2. Per stock % from open
for symbol in quotes:
    pct = (quotes[symbol].last_price - quotes[symbol].ohlc.open) / quotes[symbol].ohlc.open * 100

# 3. Per sector average
sector_score["Banking"] = avg(pct["HDFCBANK"], pct["ICICIBANK"], pct["SBIN"], pct["KOTAKBANK"])
... (for all 11 sectors)

# 4. Rank 1–11 (1 = strongest positive momentum)
sorted_sectors = sorted(sector_scores, key=lambda s: sector_scores[s], reverse=True)

# 5. Apply macro adjustments from sector-macro-map.md, then re-sort
```
