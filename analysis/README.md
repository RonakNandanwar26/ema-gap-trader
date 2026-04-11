# analysis/

Reference scripts for re-running the strategy backtest sweeps that informed
the production configuration. These are **not part of the production code** —
they exist as templates for future strategy analysis and as a reproducibility
trail for the empirical findings already shipped to `strategy.py` and the
`project_nifty_tuning_findings.md` memory note.

All scripts are read-only on the database (sqlite cache) and write nothing.
Run from the **repo root**, not from inside `analysis/`:

```bash
.venv/bin/python analysis/scenario1_gap_exit.py
```

(The scripts add the repo root to `sys.path` themselves, so this works without
setting `PYTHONPATH`.)

## Scripts

### `scenario1_gap_exit.py` — exit-rule threshold sweep template
**What it does:** Wraps `check_exit` with an additional `gap_contract` rule
that exits when `ema_gap_pct` falls below a configurable floor, then sweeps
the floor across `{None, 0.02, 0.03, 0.05, 0.08, 0.10}`.

**What it proved:** `gap_floor=0.05` gives +Rs.40k P&L with same drawdown as
baseline; `gap_floor=0.10` gives +Rs.70k P&L with PF jumping 2.50 → 3.10. The
mechanism isn't "save losers" — it frees the cooldown clock so the strategy
takes more entries on fresher trends. **This is the experiment that birthed
the `EMA_GAP_FLOOR` env var feature.**

**When to re-run:**
- Quarterly, to verify the conclusion still holds as the backtest range
  extends with newer data
- When you propose ANY new exit rule — copy this script as the template

### `combo_sweep.py` — parameter interaction template
**What it does:** Tests whether two independent strategy improvements
(`extra_entry_mode` choice and `gap_floor` exit) stack profitably. Walks 8
combinations of `mode ∈ {none, midtrend, expanding}` × `gap_floor ∈ {None,
0.05, 0.10}`.

**What it proved:** The two mechanisms are independent — gap_floor=0.10
boosts PF in every mode (pure crossover 4.09 → 4.97, midtrend 2.50 → 3.10).
For midtrend it works by freeing cooldown (650 → 1015 trades). For pure
crossover it works by cutting average loss (~₹3,200 → ~₹1,800) without
adding new trades.

**When to re-run:**
- Whenever you have TWO new strategy ideas and want to know if they
  interact constructively, destructively, or independently
- Most reusable template in this folder — copy this for any "does X stack
  with Y" question

### `scenario3_mode_sweep.py` — strategy variant comparison template
**What it does:** Runs NIFTY baseline backtest across 6 (mode, gap_min)
pairs and reports trade count, win rate, P&L, profit factor, max
drawdown, average win/loss for each.

**What it proved:** Pure crossover (`extra_entry_mode=none`, `gap_min=0.03`)
has the best risk-adjusted profile — PF 4.09, MaxDD ₹13.8k — but only ~37
trades over 3 years (1/month). Midtrend adds volume at the cost of PF and
DD. `expanding` mode adds almost zero value over `midtrend` at the same
gap_min. Higher `gap_min` is strictly worse.

**When to re-run:**
- When comparing 3+ strategy variants side-by-side
- When deciding between volume vs. risk-adjusted returns
- Good template for any future "A vs B vs C with shared baseline metrics"
  question

## What was thrown away (and why)

The following analysis scripts existed in this folder briefly but were
deleted because they were either debunked, one-shot, or covered by the
templates above:

- `rsi_ceiling_sweep.py` — debunked: blocking high-RSI entries cost ₹138k–334k
- `exit_analysis.py` / `exit_sweep.py` — diagnostics; covered by scenario1
- `sl_analysis.py` — debunked: every fixed-% premium SL was net negative
- `scenario2_hedge.py` — structurally impossible (opposite signal IS the
  primary's exit signal)
- `scenario4_trail_sl.py` — debunked: every (activation, trail) pair lost
- `window_backtest.py` / `window_gap_floor.py` — one-shot 2-week comparisons
- `smoke_test.py` — covered by `tests/test_strategy.py::TestCheckExit`
  (the `gap_contract` tests)

Full list of debunked hypotheses with reasoning is in
`project_nifty_tuning_findings.md` in the auto-memory directory.

## Adding a new analysis script

1. Copy whichever existing script is closest to your goal
2. Update the docstring with the question you're testing
3. Run from repo root
4. If the result is **important enough to influence production**, document
   it in `project_nifty_tuning_findings.md` (memory note) and consider
   shipping the change to `strategy.py` / `config.py`
5. If it's a one-shot answer, **delete the script after** — don't leave
   exploration code lying around
