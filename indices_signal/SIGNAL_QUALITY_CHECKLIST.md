# indices_signal — Signal quality checklist

Use this before tightening rules, adding symbols, sizing up, or building a backtester.
Same engine already covers **GOLD**, **US30**, and **US100**.

---

## Already in place

- [x] 4H primary + daily trend (EMA50)
- [x] min_score 75, ADX ≥ 25, per-ticker min ATR
- [x] RSI soft zone + hard veto (BUY >70 / SELL <30)
- [x] Stale-bar veto (max 6h after 4H close)
- [x] Same-direction cooldown 8h
- [x] GOLD: GC=F rollover window + volume-ratio veto
- [x] Outcome tracker (TP / SL / EXPIRED) + track record after ≥10 resolved
- [x] Weekday-aware market hours

---

## 1. Data quality

- [ ] Price source understood: GOLD = GC=F futures (not spot); US30/US100 = Yahoo indices (not broker CFD)
- [ ] ≥100 closed 4H bars and ≥50 daily bars per symbol
- [ ] GOLD 4H volume populated (volume veto depends on it)
- [ ] Timestamps in `YYYY-MM-DD HH:MM:SS` (outcome tracker needs this)

## 2. Signal logic

- [ ] Validate or raise `min_score` (75 is reachable without RSI)
- [ ] Optional: require daily trend **and** 4H EMA20/50 alignment
- [ ] Revisit `min_adx` and per-ticker `min_atr` after outcome stats exist
- [ ] RSI buy/sell zones still make sense vs live outcomes
- [ ] Policy on quick flips (opposite signal inside cooldown window)

## 3. Risk geometry

- [ ] ATR multiples (SL 2.0× / TP 3.5×) match how you trade
- [ ] 1–2% sizing stays messaging-only until outcomes justify real size
- [ ] Signal expiry (~8h in message) aligned with outcome window (up to 7 days)

## 4. Outcome evidence (before changing rules)

- [ ] ≥10–20 resolved outcomes per symbol (or overall)
- [ ] TP rate reviewed by symbol and score band
- [ ] Logs show stale / rollover / thin-volume / RSI vetoes blocking bad setups
- [ ] No rule changes based only on a handful of live signals

## 5. Ops

- [ ] Pipeline healthy: collector → telegram_bot → outcome_tracker
- [ ] Yahoo circuit breaker working; cron not over-fetching
- [ ] Preflight / `check_all.sh` after config changes
- [ ] Stage logs retained for “why signal / why no signal” audits

## 6. Same engine for gold and others?

**Yes.** GOLD / US30 / US100 share scoring and indicators.

| Symbol | Extra guards |
|--------|----------------|
| GOLD   | Rollover, volume ratio, futures disclaimer |
| US30   | Cash-session hours |
| US100  | Cash-session hours |

To add more symbols: extend `TICKERS` (`yahoo_symbol`, `min_atr`, optional ATR multipliers) + market hours in `util.is_market_open` + any symbol-specific vetoes.

---

## Suggested order

1. Stabilize data + outcomes (pipeline runs, outcomes resolve).
2. Review first 10–20 resolved signals (symbol, score, reasons).
3. Tighten only what the data supports.
4. Then historical backtester reusing `analyze_ticker` logic.
5. Then more symbols if GOLD / US30 / US100 look acceptable.
