# Research Results — Premium vs Spot Indicators & Parameter Optimization

**Date**: 2026-04-12
**Instrument**: NIFTY | 15-min candles | 2023-04-01 to 2026-03-28 (~3 years)
**Current config**: EMA 9/21 | ST 10/3.0 | midtrend | gap_min=0.03 | gap_max=0.5 | ORB 30min | cooldown=3 | max_hold=20 | gap_floor=0 | RSI cutoff=50

---

## 1. Hybrid Exit Test — Spot Entry + Strike Premium Exit

**Hypothesis**: Use spot indicators for entry but exit when the option premium's own indicators signal weakness (catching sideways theta/IV decay that spot misses).

### Test 1a: 5-min premium candles (all exit signals)

Premium EMA cross + ST flip + RSI weakness as exit signals on 5-min strike candles.

| Metric | Spot (current) | Strike exit (5-min) |
|---|---|---|
| Trades | 641 | 641 |
| Win Rate | 51% | 50% |
| Net P&L | Rs.+10.89L | Rs.+5.85L |
| Profit Factor | 2.50 | 2.44 |
| Avg Win | Rs.5,588 | Rs.3,098 |
| Avg Loss | Rs.2,302 | Rs.1,266 |
| Max Drawdown | Rs.24,856 | Rs.28,629 |
| Max Loss Streak | 8 | 11 |
| Green Months | 34/36 | 32/36 |

**Conclusion**: 5-min premium candles too noisy. strike_EMA_cross (19% WR, -99K) killed it. Dropped.

### Test 1b: 5-min premium candles (ST flip + RSI weak only, no EMA cross)

| Metric | Spot (current) | Combined (ST+RSI) |
|---|---|---|
| Trades | 641 | 641 |
| Win Rate | 51% | 61% |
| Net P&L | Rs.+10.89L | Rs.+5.40L |
| Profit Factor | 2.50 | 2.57 |
| Max Drawdown | Rs.24,856 | Rs.24,541 |
| Max Loss Streak | 8 | 6 |
| Green Months | 34/36 | 30/36 |

**Conclusion**: Better WR (61%) and PF (2.57) but still cuts winners short. 285 saves vs 221 hurts.

### Test 1c: 15-min resampled premium candles (ST flip + gap contraction)

Resampled 5-min strike candles to 15-min to match system timeframe.

| Metric | Spot (current) | Combined (15-min premium) |
|---|---|---|
| Trades | 641 | 641 |
| Win Rate | 51% | **61%** |
| Net P&L | **Rs.+10.89L** | Rs.+5.40L |
| **Profit Factor** | 2.50 | **2.57** |
| **Max Drawdown** | Rs.24,856 | **Rs.24,541** |
| **Max Loss Streak** | 8 | **6** |
| Green Months | **34/36** | 30/36 |

Exit breakdown (combined):
- prem_ST_flip: 467 trades, 65% WR, Rs.+2.73L
- max_hold: 65 trades, 85% WR, Rs.+3.41L
- prem_gap_contract: 51 trades, 37% WR, Rs.-34K (noise)

**Conclusion**: 15-min resampling dramatically improved premium ST_flip (65% WR vs 1% on 5-min). Best risk metrics across all tests but lower absolute P&L.

---

## 2. Strike-Driven System — Premium Indicators for Entry AND Exit

**Hypothesis**: Fix ATM at day start, compute EMA/ST/RSI on premium candles, use premium indicators for everything. No spot indicators at all.

### Test 2a: 5-min premium candles + custom entry/exit

| Metric | Spot (current) | Strike-driven (5-min) |
|---|---|---|
| Trades | 650 | 678 |
| Win Rate | 51% | 38% |
| Net P&L | Rs.+11.01L | Rs.-82K (loss) |
| Profit Factor | 2.50 | 0.85 |
| Max Drawdown | Rs.24,856 | Rs.1,02,778 |
| Green Months | 34/36 | 10/36 |

Exit breakdown:
- prem_ST_flip: 182 trades, **1% WR**, Rs.-3.17L (catastrophic)
- max_hold: 307 trades, 52% WR, Rs.+1.51L
- time_exit: 189 trades, 49% WR, Rs.+0.83L

**Conclusion**: 5-min premium candles completely destroyed by noise. ST flip on 5-min premium is useless.

### Test 2b: 15-min resampled + ATM refresh after each trade

Same as 2a but with 15-min resampled candles and ATM recalculation mid-day.

| Metric | Spot (current) | Strike-driven (15-min) |
|---|---|---|
| Trades | 650 | 148 |
| Win Rate | 51% | 51% |
| Net P&L | Rs.+11.01L | Rs.+1.21L |
| Profit Factor | 2.50 | 2.08 |
| Max Drawdown | Rs.24,856 | Rs.20,215 |
| Max Loss Streak | 8 | 4 |

**Conclusion**: Risk metrics improved (lowest drawdown Rs.20K, loss streak 4) but only 148 trades in 3 years. Premium EMA crossovers too rare on 15-min candles.

### Test 2c: Same strategy rules on premium data (check_entry/check_exit from strategy.py)

Exact same crossover + midtrend + EMA gap + RSI + ORB rules, just computed on premium candles.

| Metric | Spot (current) | Premium-driven (15-min) |
|---|---|---|
| Trades | 650 | 14 |
| Win Rate | 51% | 36% |
| Net P&L | Rs.+11.01L | Rs.-11K |

**Conclusion**: Only 14 trades in 3 years. Premium candles have limited history (2 days) so EMAs don't generate enough crossovers. Strategy rules designed for continuous spot data don't translate to short premium series.

---

## 3. Dual-Confirmation Exit — Premium Gates Spot Exits

**Hypothesis**: Keep everything same, but ST_flip and EMA_cross exits only fire if the premium SuperTrend also confirms (bearish). If premium is still bullish, hold — the spot signal is likely false.

| Metric | Current | Dual-Confirm |
|---|---|---|
| Trades | 650 | 634 |
| Win Rate | 51% | 51% |
| Net P&L | Rs.+11.01L | Rs.+10.97L |
| Profit Factor | 2.50 | 2.42 |
| Max Drawdown | Rs.24,856 | Rs.28,363 |
| Green Months | 34/36 | **35/36** |

Exit breakdown (dual):
- EMA_cross_confirmed: 62 trades, 6% WR, Rs.-1.27L
- ST_flip_confirmed: 198 trades, 21% WR, Rs.-2.26L
- max_hold: 374 trades, 74% WR, Rs.+14.5L

**Conclusion**: Essentially a wash (Rs.-3,790 difference). The premium gate blocked 56 exits but those trades mostly lost anyway when held to max_hold. One bright spot: 35/36 green months.

---

## 4. Entry Quality — Trade Pattern Analysis

### 4a. Patterns (winners vs losers)

**Entry Type**:
- Crossover: 37 trades, **59% WR**, PF 4.09
- Midtrend: 613 trades, 50% WR, PF 2.39

**Direction**:
- CE: 344 trades, 52% WR, PF 2.66
- PE: 306 trades, 49% WR, PF 2.34

**Time of Day (30-min buckets)**:
| Time | Trades | WR | P&L | PF |
|---|---|---|---|---|
| 09:30 | 56 | **61%** | +1.09L | 3.60 |
| 10:00 | 91 | 47% | +0.54L | 1.45 |
| 10:30 | 66 | 42% | +0.90L | 2.15 |
| 11:00 | 52 | 48% | +1.07L | 2.88 |
| 11:30 | 43 | 44% | +0.52L | 2.42 |
| 12:00 | 44 | 50% | +0.60L | 2.05 |
| **12:30** | **35** | **31%** | **-0.14L** | **0.75** |
| 13:00 | 47 | 47% | +0.74L | 2.26 |
| 13:30 | 48 | 56% | +1.41L | 4.01 |
| 14:00 | 59 | 63% | +1.44L | 3.27 |
| **14:30** | **44** | **57%** | **+1.80L** | **5.37** |
| 15:00 | 49 | 59% | +1.03L | 3.18 |

**Day of Week**:
| Day | Trades | WR | P&L | PF |
|---|---|---|---|---|
| Mon | 116 | 45% | +0.23L | 1.14 |
| Tue | 129 | 46% | +1.55L | 1.98 |
| Wed | 142 | 40% | +0.74L | 1.39 |
| **Thu** | **126** | **72%** | **+6.84L** | **9.59** |
| Fri | 132 | 52% | +1.77L | 2.28 |

**EMA Gap at Entry**:
| Gap Range | Trades | WR | P&L | PF |
|---|---|---|---|---|
| 0.03-0.05% | 169 | 46% | +3.01L | 2.62 |
| 0.05-0.08% | 147 | 51% | +2.19L | 2.30 |
| 0.08-0.12% | 113 | 52% | +1.84L | 2.60 |
| **0.12-0.20%** | **130** | **55%** | **+2.94L** | **3.29** |
| 0.20-0.35% | 77 | 49% | +0.86L | 1.76 |
| 0.35-0.50% | 14 | 50% | +0.17L | 1.66 |

**Premium at Entry**:
| Premium | Trades | WR | P&L | PF |
|---|---|---|---|---|
| **Rs.0-50** | **115** | **77%** | **+6.86L** | **18.62** |
| Rs.50-100 | 256 | 44% | +2.91L | 2.00 |
| Rs.100-150 | 178 | 48% | +0.86L | 1.39 |
| Rs.150-200 | 75 | 47% | +0.55L | 1.42 |
| Rs.200-300 | 25 | 28% | -0.19L | 0.65 |

**Exit Reason**:
| Reason | Trades | WR | P&L | PF |
|---|---|---|---|---|
| EMA_cross | 87 | 8% | -1.81L | 0.18 |
| ST_flip | 229 | 23% | -2.10L | 0.50 |
| **max_hold** | **334** | **81%** | **+14.92L** | **16.73** |

**Candles Held**:
| Candles | Trades | WR | P&L |
|---|---|---|---|
| 1-4 | 32 | 19% | -0.26L |
| 5-9 | 101 | 15% | -1.50L |
| 10-14 | 80 | 16% | -1.27L |
| 15-19 | 87 | 24% | -0.90L |
| **20** | **350** | **78%** | **+14.94L** |

**Trades Per Day**:
| Count | Days | WR | PF |
|---|---|---|---|
| 1/day | 512 trades | 54% | 3.03 |
| 2/day | 138 trades | 40% | 1.26 |

**Month**:
| Month | Trades | WR | P&L |
|---|---|---|---|
| Jan | 63 | 40% | +0.54L |
| Feb | 57 | 44% | +0.59L |
| Mar | 43 | 56% | +0.78L |
| Apr | 47 | 51% | +0.77L |
| May | 56 | 52% | +1.24L |
| Jun | 48 | 58% | +1.30L |
| Jul | 60 | 42% | +0.64L |
| Aug | 55 | 53% | +0.34L |
| Sep | 51 | 55% | +0.99L |
| Oct | 57 | 54% | +1.22L |
| Nov | 53 | 47% | +1.00L |
| Dec | 60 | 60% | +1.58L |

### Key pattern insights:
1. **Cheap premiums (Rs.0-50) = gold**: 77% WR, PF 18.62
2. **Thursday dominates**: 72% WR, PF 9.59 (likely weekly expiry effect)
3. **2nd trade of the day hurts**: WR drops from 54% to 40%
4. **12:30 is the worst time**: 31% WR, PF 0.75
5. **14:30 is the best time**: 57% WR, PF 5.37
6. **All P&L comes from max_hold**: 81% WR, Rs.+14.92L

---

## 5. Parameter Sweeps

### 5a. EMA Gap Minimum

| Value | Trades | WR | P&L | PF |
|---|---|---|---|---|
| **0 (none)** | 684 | 51% | **Rs.+12.3L** | **2.67** |
| 0.01 | 673 | 50% | Rs.+11.9L | 2.59 |
| 0.02 | 660 | 50% | Rs.+11.9L | 2.60 |
| 0.03 (current) | 650 | 51% | Rs.+11.0L | 2.50 |
| 0.05 | 615 | 51% | Rs.+10.0L | 2.39 |
| 0.10 | 470 | 51% | Rs.+7.0L | 2.21 |

**Winner**: gap_min=0 — remove the filter entirely.

### 5b. Entry Mode

| Mode | Trades | WR | P&L | PF |
|---|---|---|---|---|
| none (crossover only) | 37 | 59% | Rs.+1.49L | **4.09** |
| **midtrend (current)** | 650 | 51% | **Rs.+11.0L** | 2.50 |
| expanding | 619 | 50% | Rs.+10.6L | 2.51 |

**Winner**: midtrend for P&L, none for PF. Keep midtrend.

### 5c. ORB Filter

| Setting | Trades | WR | P&L | PF |
|---|---|---|---|---|
| **on (current)** | 650 | 51% | Rs.+11.0L | **2.50** |
| off | 884 | 48% | **Rs.+13.7L** | 2.32 |

**Trade-off**: ORB off = +Rs.2.7L more P&L but lower PF.

### 5d. Cooldown Candles

| Value | Trades | WR | P&L | PF |
|---|---|---|---|---|
| 0 | 727 | 51% | **Rs.+13.0L** | 2.60 |
| 1 | 705 | 52% | Rs.+12.9L | 2.65 |
| **2** | 677 | 52% | Rs.+12.9L | **2.75** |
| 3 (current) | 650 | 51% | Rs.+11.0L | 2.50 |
| 5 | 622 | 49% | Rs.+10.3L | 2.43 |
| 7 | 592 | 50% | Rs.+10.1L | 2.50 |

**Winner**: cooldown=2 — best PF (2.75).

### 5e. Max Hold Candles

| Value | Trades | WR | P&L | PF |
|---|---|---|---|---|
| 8 | 1031 | 54% | Rs.+10.9L | 2.34 |
| 10 | 931 | 54% | Rs.+11.6L | 2.45 |
| 12 | 847 | 51% | Rs.+11.3L | 2.45 |
| **15** | 763 | 53% | **Rs.+12.8L** | **2.63** |
| 18 | 701 | 51% | Rs.+11.6L | 2.51 |
| 20 (current) | 650 | 51% | Rs.+11.0L | 2.50 |
| 25 | 610 | 46% | Rs.+9.3L | 2.18 |
| 30 | 557 | 46% | Rs.+10.1L | 2.39 |

**Winner**: max_hold=15 — best P&L AND PF.

### 5f. EMA Gap Floor (exit)

| Value | Trades | WR | P&L | PF |
|---|---|---|---|---|
| 0 (current) | 650 | 51% | Rs.+11.0L | 2.50 |
| 0.02 | 666 | 48% | Rs.+10.9L | 2.47 |
| 0.05 | 724 | 49% | Rs.+11.4L | 2.77 |
| 0.08 | 894 | 51% | Rs.+10.4L | 2.78 |
| **0.10** | 1015 | 52% | **Rs.+11.7L** | **3.10** |
| 0.12 | 1156 | 53% | Rs.+10.4L | 2.78 |
| 0.15 | 1313 | 55% | Rs.+11.1L | 2.90 |

**Winner**: gap_floor=0.10 — PF jumps from 2.50 to 3.10 (single biggest improvement).

### 5g. EMA Spans

| Spans | Trades | WR | P&L | PF |
|---|---|---|---|---|
| **5/13** | 724 | 47% | **Rs.+11.6L** | **2.63** |
| 5/21 | 691 | 48% | Rs.+11.9L | 2.61 |
| 8/21 | 658 | 50% | Rs.+11.7L | 2.57 |
| 9/21 (current) | 650 | 51% | Rs.+11.0L | 2.50 |
| 9/26 | 640 | 51% | Rs.+11.1L | 2.51 |
| 13/21 | 589 | 52% | Rs.+9.6L | 2.39 |
| 13/34 | 578 | 52% | Rs.+9.8L | 2.49 |

**Winner**: 5/13 or 5/21 — faster EMAs catch trends earlier.

### 5h. SuperTrend Params

| Period/Mult | Trades | WR | P&L | PF |
|---|---|---|---|---|
| 7/2.0 | 696 | 51% | Rs.+11.8L | 2.72 |
| 7/3.0 | 652 | 50% | Rs.+11.1L | 2.49 |
| **10/2.0** | 699 | 52% | **Rs.+11.9L** | **2.70** |
| 10/3.0 (current) | 650 | 51% | Rs.+11.0L | 2.50 |
| 10/4.0 | 609 | 53% | Rs.+10.5L | 2.40 |
| 14/3.0 | 654 | 52% | Rs.+11.3L | 2.53 |
| 14/4.0 | 607 | 52% | Rs.+9.8L | 2.29 |

**Winner**: 10/2.0 — more sensitive SuperTrend.

### 5i. RSI Cutoff

| Value | Trades | WR | P&L | PF |
|---|---|---|---|---|
| 40 | 632 | 50% | **Rs.+10.9L** | 2.50 |
| 45 | 632 | 50% | Rs.+10.9L | 2.50 |
| 48 | 631 | 51% | Rs.+10.9L | 2.51 |
| **50 (current)** | 629 | 50% | Rs.+10.9L | **2.52** |
| 52 | 627 | 50% | Rs.+10.8L | 2.51 |
| 55 | 621 | 50% | Rs.+10.3L | 2.43 |
| 60 | 609 | 50% | Rs.+9.9L | 2.38 |

**Winner**: 50 is already optimal. Minimal impact either way.

### 5j. EMA Gap Maximum

| Value | Trades | WR | P&L | PF |
|---|---|---|---|---|
| 0.3% | 613 | 51% | Rs.+10.9L | **2.59** |
| 0.4% | 625 | 50% | Rs.+10.9L | 2.54 |
| **0.5% (current)** | 629 | 50% | Rs.+10.9L | 2.52 |
| 0.6% | 630 | 50% | **Rs.+10.9L** | 2.53 |
| 0.8%+ | 631-632 | 50% | Rs.+10.9L | 2.49-2.50 |

**Winner**: 0.5% is fine. Minimal impact.

### 5k. ORB Window

| Window | Trades | WR | P&L | PF |
|---|---|---|---|---|
| 15min | 650 | 51% | Rs.+11.0L | 2.50 |
| 20min | 650 | 51% | Rs.+11.0L | 2.50 |
| 30min (current) | 629 | 50% | Rs.+10.9L | 2.52 |
| **45min** | 595 | **53%** | **Rs.+11.7L** | **2.84** |
| 60min | 573 | 51% | Rs.+11.1L | 2.72 |

**Winner**: 45min — wider ORB range filters false breakouts better.

---

## 6. Combined Optimal Configs

### With ORB on (30min)

| Config | Trades | WR | P&L | PF |
|---|---|---|---|---|
| **Current** | 650 | 51% | Rs.+11.0L | 2.50 |
| Optimal config only | 1309 | 53% | Rs.+14.5L | 3.06 |
| **Optimal + EMA 5/13 + ST 10/2.0** | 1579 | 56% | Rs.+15.1L | 3.30 |
| Optimal + EMA 5/21 + ST 10/2.0 | 1230 | 54% | Rs.+15.9L | 3.35 |

### With ORB variations

| Config | Trades | WR | P&L | PF |
|---|---|---|---|---|
| Optimal + ORB 30min | 1579 | 56% | Rs.+15.1L | 3.30 |
| Optimal + ORB 45min | 1442 | **57%** | Rs.+14.1L | 3.41 |
| Optimal + ORB 60min | 1309 | **57%** | Rs.+13.2L | **3.44** |
| **Optimal + ORB off** | 2932 | 52% | **Rs.+19.2L** | 2.53 |

---

## 7. Intraday vs Carry-Forward

All using optimal config: EMA 5/13 + ST 10/2.0 + gap_min=0 + cooldown=2 + max_hold=15 + gap_floor=0.10 + ORB 30min

| Mode | Trades | WR | P&L | PF | Avg Hold |
|---|---|---|---|---|---|
| **Intraday** (exit 15:10) | 1634 | 58% | Rs.+11.0L | 3.22 | 3.8 candles |
| **Carry-forward (max_hold=15)** | 1697 | 57% | **Rs.+16.1L** | **3.37** | 4.4 candles |
| Carry-forward (max_hold=30) | 1628 | 57% | Rs.+14.6L | 3.29 | 4.7 candles |
| Carry-forward (max_hold=50) | 1622 | 56% | Rs.+14.1L | 3.19 | 4.8 candles |

Intraday exit breakdown:
- gap_contract: 1137 trades, 56% WR, Rs.+3.18L
- max_hold: 62 trades, 98% WR, Rs.+4.17L
- time_exit: 298 trades, 69% WR, Rs.+4.27L
- ST_flip: 107 trades, 30% WR, Rs.-0.44L

Carry-forward (max_hold=15) exit breakdown:
- gap_contract: 1341 trades, 57% WR, Rs.+6.81L
- max_hold: 118 trades, 93% WR, Rs.+9.41L
- ST_flip: 196 trades, 37% WR, Rs.+0.09L

**Conclusion**: Carry-forward with max_hold=15 is best — Rs.+16.1L vs Rs.+11.0L intraday (+46%). Trades that were force-closed at 15:10 continue overnight and many hit max_hold the next day at 93% WR.

---

## 8. Final Recommendation

### Best absolute P&L:
**Optimal + ORB off + carry-forward**: Rs.+19.2L, PF 2.53, 52% WR, 2932 trades

### Best risk-adjusted:
**Optimal + ORB 60min + carry-forward**: Rs.+13.2L, PF 3.44, 57% WR, 1309 trades

### Best balance (recommended):
**Optimal + ORB 30min + carry-forward**: Rs.+16.1L, PF 3.37, 57% WR, 1697 trades

### Optimal parameter changes vs current:

| Parameter | Current | Optimal | Impact |
|---|---|---|---|
| EMA spans | 9/21 | **5/13** | Faster trend detection |
| SuperTrend | 10/3.0 | **10/2.0** | More sensitive |
| EMA gap min | 0.03 | **0** | Remove unnecessary filter |
| Cooldown | 3 | **2** | PF 2.50 → 2.75 |
| Max hold | 20 | **15** | Best P&L + PF sweet spot |
| Gap floor exit | 0 | **0.10** | Biggest single improvement (PF 2.50 → 3.10) |
| Time exit | 15:10 | **none** | +46% P&L from carry-forward |
| ORB window | 30min | **30-45min** | 45min improves PF if ORB kept on |

### Overall improvement:
- P&L: Rs.+11.0L → Rs.+16.1L (**+46%**)
- PF: 2.50 → 3.37 (**+35%**)
- WR: 51% → 57% (**+6%**)
