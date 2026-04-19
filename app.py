"""app.py — Streamlit dashboard: Backtest | Paper Trading | Live Trading."""

from __future__ import annotations

import copy
import logging
import os
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from log_setup import setup_logging
setup_logging()

import plotly.io as pio

from backtester import run_backtest
from config import StrategyConfig, InstrumentConfig, INSTRUMENTS, get_dhan_db_path, get_data_dir
from dhan_data import (
    _EXPIRY_FLAGS,
    _get_cached_date_range,
    _resolve_incremental_range,
    fetch_index_intraday,
    init_dhan,
    prefetch_option_data,
)
from persistence import TradeStore
from results import compute_stats, compute_equity_curve
from trader import Trader

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(page_title="EMA Gap Trader", page_icon=":chart_with_upwards_trend:", layout="wide")

# ---------------------------------------------------------------------------
# Dhan token sidebar widget — daily JWT rotation without service restart
# ---------------------------------------------------------------------------
from pathlib import Path as _DhanPath

_DHAN_TOKEN_FILE = _DhanPath(os.environ.get("DHAN_TOKEN_PATH", "/etc/ema-trader/dhan.token"))

with st.sidebar.expander("Dhan token (daily refresh)", expanded=False):
    if _DHAN_TOKEN_FILE.exists() and _DHAN_TOKEN_FILE.read_text().strip():
        st.caption(f"Status: SET ({_DHAN_TOKEN_FILE})")
    else:
        st.caption(f"Status: MISSING ({_DHAN_TOKEN_FILE})")
    _new_dhan_token = st.text_input(
        "Paste new Dhan access token",
        type="password",
        key="dhan_token_input",
    )
    if st.button("Save token", key="dhan_token_save"):
        if not _new_dhan_token.strip():
            st.error("Empty token — not saved.")
        else:
            try:
                _DHAN_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
                _DHAN_TOKEN_FILE.write_text(_new_dhan_token.strip())
                st.success("Saved. Next Dhan call will use it.")
            except PermissionError as exc:
                st.error(f"Permission denied writing {_DHAN_TOKEN_FILE}: {exc}")

# ---------------------------------------------------------------------------
# Trading session wrapper (runs Trader in daemon thread)
# ---------------------------------------------------------------------------


@dataclass
class TradingStatus:
    is_running: bool
    current_equity: float
    initial_capital: float
    trade_count: int
    open_trade: Optional[dict]
    recent_trades: list[dict] = field(default_factory=list)
    error: Optional[str] = None


class TradingSession:
    def __init__(self, strat_config: StrategyConfig, inst_config: InstrumentConfig, capital: int, live: bool = False, variant: str = ""):
        self.sc = strat_config
        self.ic = inst_config
        self.capital = capital
        self.live = live
        self.variant = variant
        store_key = f"{inst_config.name}-{variant}" if variant else inst_config.name
        self._store = TradeStore(store_key, "live" if live else "paper", get_data_dir())
        self._trader: Trader | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._error: str | None = None
        self._user_stopped = False

    @property
    def is_running(self) -> bool:
        if self._user_stopped:
            return False
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._error = None
        self._user_stopped = False
        self._trader = Trader(self.sc, self.ic, self.capital, live=self.live, trade_store=self._store, label=self.variant)
        self._thread = threading.Thread(target=self._run, daemon=True, name="trader-thread")
        self._thread.start()

    def stop(self) -> None:
        self._user_stopped = True
        with self._lock:
            if self._trader:
                self._trader.running = False

    def get_status(self) -> TradingStatus:
        with self._lock:
            if self._trader is None:
                # No active trader — show historical data from store
                historical = self._store.load_trades()
                saved_equity = self._store.load_equity()
                equity = saved_equity if saved_equity is not None else float(self.capital)
                return TradingStatus(
                    is_running=self.is_running,
                    current_equity=equity,
                    initial_capital=float(self.capital),
                    trade_count=len(historical),
                    open_trade=None,
                    recent_trades=historical[-5:],
                    error=self._error,
                )
            t = self._trader
            return TradingStatus(
                is_running=self.is_running,
                current_equity=t.current_equity,
                initial_capital=t.initial_capital,
                trade_count=len(t.trades),
                open_trade=copy.deepcopy(t.open_trade),
                recent_trades=[copy.deepcopy(tr) for tr in t.trades[-5:]],
                error=self._error,
            )

    def _run(self) -> None:
        try:
            self._trader.run()
        except Exception:
            self._error = traceback.format_exc()
            logging.exception("Trading session crashed")


# ---------------------------------------------------------------------------
# HTML report generation
# ---------------------------------------------------------------------------


def _generate_html_report(stats: dict, curve: list, trades: list, sc: StrategyConfig, ic: InstrumentConfig, capital: float, start_date, end_date) -> str:
    """Generate a standalone HTML report matching the Streamlit dashboard design."""

    # Build equity chart as embedded HTML
    chart_html = ""
    if len(curve) > 1:
        curve_df = pd.DataFrame(curve[1:], columns=["timestamp", "equity"])
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=curve_df["timestamp"], y=curve_df["equity"],
                                  mode="lines", fill="tozeroy", name="Equity",
                                  line=dict(color="#4CAF50")))
        fig.update_layout(title="Equity Curve", xaxis_title="Date", yaxis_title="Equity (Rs.)",
                          height=400, template="plotly_dark",
                          paper_bgcolor="#1a1a2e", plot_bgcolor="#16213e",
                          font=dict(color="#e0e0e0"))
        chart_html = pio.to_html(fig, full_html=False, include_plotlyjs="cdn")

    # Helper to build table HTML
    def table(headers, rows):
        h = "".join(f"<th>{h}</th>" for h in headers)
        body = ""
        for row in rows:
            cells = "".join(f"<td>{c}</td>" for c in row)
            body += f"<tr>{cells}</tr>\n"
        return f"<table><thead><tr>{h}</tr></thead><tbody>{body}</tbody></table>"

    # Entry type breakdown
    et_table = ""
    if stats.get("entry_type_breakdown"):
        rows = []
        for et, d in sorted(stats["entry_type_breakdown"].items()):
            wr = d["w"] / d["n"] * 100 if d["n"] else 0
            rows.append([et, d["n"], f"{wr:.0f}%", f"Rs.{d['pnl']:,.0f}"])
        et_table = f"<h2>Entry Type Breakdown</h2>{table(['Type', 'Trades', 'WR', 'P&L'], rows)}"

    # Exit breakdown
    exit_rows = []
    for reason, d in sorted(stats["exit_breakdown"].items()):
        wr = d["w"] / d["n"] * 100 if d["n"] else 0
        exit_rows.append([reason, d["n"], f"{wr:.0f}%", f"Rs.{d['pnl']:,.0f}"])
    exit_table = table(["Reason", "Trades", "WR", "P&L"], exit_rows)

    # Yearly
    yr_rows = []
    for y, d in sorted(stats["yearly"].items()):
        wr = d["w"] / d["n"] * 100 if d["n"] else 0
        yr_rows.append([y, d["n"], f"{wr:.0f}%", f"Rs.{d['pnl']:,.0f}"])
    yr_table = table(["Year", "Trades", "WR", "P&L"], yr_rows)

    # Monthly
    mo_rows = []
    for m, v in sorted(stats["monthly"].items()):
        color = "#4CAF50" if v >= 0 else "#f44336"
        mo_rows.append([m, f"<span style='color:{color}'>Rs.{v:,.0f}</span>"])
    mo_table = table(["Month", "P&L"], mo_rows)

    # Direction
    dir_html = ""
    if stats.get("direction"):
        dir_rows = []
        for d_name in ("CE", "PE"):
            if d_name in stats["direction"]:
                d = stats["direction"][d_name]
                wr = d["w"] / d["n"] * 100 if d["n"] else 0
                dir_rows.append([d_name, d["n"], f"{wr:.0f}%", f"Rs.{d['pnl']:,.0f}"])
        if dir_rows:
            dir_html = f"<h2>Direction Breakdown</h2>{table(['Dir', 'Trades', 'WR', 'P&L'], dir_rows)}"

    # Trade log
    trade_rows = []
    for t in trades:
        if t.get("prem_pnl") is not None:
            pnl_color = "#4CAF50" if t["prem_pnl"] >= 0 else "#f44336"
            trade_rows.append([
                str(t.get("entry", ""))[:19],
                str(t.get("exit", ""))[:19],
                t.get("dir", ""),
                f"{t.get('spot_in', 0):.1f}" if t.get("spot_in") else "",
                f"{t.get('strike', 0):.0f}" if t.get("strike") else "",
                f"{t.get('prem_in', 0):.1f}" if t.get("prem_in") else "",
                f"{t.get('prem_out', 0):.1f}" if t.get("prem_out") else "",
                f"<span style='color:{pnl_color}'>Rs.{t['prem_pnl']:,.0f}</span>",
                t.get("reason", ""),
                t.get("entry_type", ""),
            ])
    trade_table = table(
        ["Entry", "Exit", "Dir", "Spot In", "Strike", "Prem In", "Prem Out", "P&L", "Reason", "Entry Type"],
        trade_rows,
    )

    pnl_color = "#4CAF50" if stats["pnl"] >= 0 else "#f44336"
    ret_color = "#4CAF50" if stats["return_pct"] >= 0 else "#f44336"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>EMA Gap Trader — Backtest Report</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: 'Segoe UI', system-ui, -apple-system, sans-serif; background: #0e1117; color: #e0e0e0; padding: 24px; }}
  .container {{ max-width: 1200px; margin: 0 auto; }}
  h1 {{ color: #fff; margin-bottom: 8px; font-size: 28px; }}
  .subtitle {{ color: #888; margin-bottom: 24px; font-size: 14px; }}
  h2 {{ color: #ccc; margin: 32px 0 12px; font-size: 20px; border-bottom: 1px solid #333; padding-bottom: 8px; }}
  .config {{ background: #1a1a2e; border-radius: 8px; padding: 16px; margin-bottom: 24px; display: flex; flex-wrap: wrap; gap: 24px; }}
  .config-item {{ font-size: 13px; }}
  .config-item .label {{ color: #888; }}
  .config-item .value {{ color: #fff; font-weight: 600; }}
  .metrics {{ display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px; margin-bottom: 24px; }}
  .metric {{ background: #1a1a2e; border-radius: 8px; padding: 16px; text-align: center; }}
  .metric .label {{ font-size: 12px; color: #888; text-transform: uppercase; margin-bottom: 4px; }}
  .metric .value {{ font-size: 22px; font-weight: 700; color: #fff; }}
  .chart {{ margin: 24px 0; }}
  table {{ width: 100%; border-collapse: collapse; margin-bottom: 8px; font-size: 13px; }}
  th {{ background: #1a1a2e; color: #aaa; text-align: left; padding: 10px 12px; font-weight: 600; text-transform: uppercase; font-size: 11px; }}
  td {{ padding: 8px 12px; border-bottom: 1px solid #222; }}
  tr:hover {{ background: #1a1a2e; }}
  .section {{ margin-bottom: 32px; }}
  @media (max-width: 768px) {{
    .metrics {{ grid-template-columns: repeat(3, 1fr); }}
  }}
</style>
</head>
<body>
<div class="container">
  <h1>EMA Gap Trader — Backtest Report</h1>
  <p class="subtitle">{ic.name} | {start_date} to {end_date} | Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>

  <div class="config">
    <div class="config-item"><span class="label">Mode:</span> <span class="value">crossover + {sc.extra_entry_mode}</span></div>
    <div class="config-item"><span class="label">Gap Min:</span> <span class="value">{sc.ema_gap_min}%</span></div>
    <div class="config-item"><span class="label">Max Hold:</span> <span class="value">{sc.max_hold_candles} candles</span></div>
    <div class="config-item"><span class="label">Cooldown:</span> <span class="value">{sc.cooldown_candles} candles</span></div>
    <div class="config-item"><span class="label">Interval:</span> <span class="value">{sc.candle_interval}min</span></div>
    <div class="config-item"><span class="label">Capital:</span> <span class="value">Rs.{capital:,.0f}</span></div>
  </div>

  <div class="metrics">
    <div class="metric"><div class="label">Trades</div><div class="value">{stats['n']}</div></div>
    <div class="metric"><div class="label">Win Rate</div><div class="value">{stats['wr']:.0f}%</div></div>
    <div class="metric"><div class="label">Net P&L</div><div class="value" style="color:{pnl_color}">Rs.{stats['pnl']:,.0f}</div></div>
    <div class="metric"><div class="label">Profit Factor</div><div class="value">{stats['pf']:.2f}</div></div>
    <div class="metric"><div class="label">Max Drawdown</div><div class="value" style="color:#f44336">Rs.{stats['max_dd']:,.0f}</div></div>
    <div class="metric"><div class="label">Return</div><div class="value" style="color:{ret_color}">{stats['return_pct']:.1f}%</div></div>
  </div>

  <div class="chart">{chart_html}</div>

  {et_table}

  <div class="section">
    <h2>Exit Breakdown</h2>
    {exit_table}
  </div>

  {dir_html}

  <div class="section">
    <h2>Yearly Summary</h2>
    {yr_table}
  </div>

  <div class="section">
    <h2>Monthly P&L</h2>
    {mo_table}
  </div>

  <div class="section">
    <h2>Trade Log ({len(trades)} trades)</h2>
    {trade_table}
  </div>
</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Session state init
# ---------------------------------------------------------------------------

if "backtest_results" not in st.session_state:
    st.session_state.backtest_results = None
if "paper_sessions" not in st.session_state:
    st.session_state.paper_sessions = {}  # instrument_name -> TradingSession
if "live_sessions" not in st.session_state:
    st.session_state.live_sessions = {}   # instrument_name -> TradingSession
if "live_confirmed" not in st.session_state:
    st.session_state.live_confirmed = False
if "opt_sessions" not in st.session_state:
    st.session_state.opt_sessions = {}  # instrument_name -> TradingSession (optimized)

# ---------------------------------------------------------------------------
# Sidebar — configurable params
# ---------------------------------------------------------------------------

with st.sidebar:
    st.title("EMA Gap Trader")
    st.markdown("---")

    instrument = st.selectbox("Instrument", ["NIFTY", "BANKNIFTY", "SENSEX"])
    extra_mode = st.selectbox("Extra Entry Mode", ["midtrend", "expanding", "none"],
                              help="Crossover is always active. This adds mid-trend re-entries.")
    gap_min = st.number_input("EMA Gap Min %", min_value=0.0, max_value=1.0, value=0.03, step=0.01, format="%.2f")
    max_hold = st.number_input("Max Hold Candles", min_value=5, max_value=50, value=20)
    cooldown = st.number_input("Cooldown Candles", min_value=1, max_value=10, value=3)
    candle_interval = st.selectbox("Candle Interval (min)", [15, 5])
    max_capital_pct = st.slider("Max Capital Per Trade %", min_value=5, max_value=50, value=25, step=5,
                                help="Max % of equity to spend on a single trade. Moves to OTM strike if ATM exceeds budget.")
    orb_filter = st.checkbox("ORB Filter", value=True,
                             help="Only enter CE above opening range high, PE below opening range low (first 30 min).")
    ema_gap_floor = st.number_input(
        "EMA Gap Floor Exit %",
        min_value=0.0, max_value=1.0,
        value=float(os.getenv("EMA_GAP_FLOOR", "0.0")),
        step=0.01, format="%.2f",
        help="Exit when ema_gap_pct drops below this floor after 2+ candles held. 0 disables. "
             "Default reads EMA_GAP_FLOOR env var.",
    )
    variant_gap_floor = st.number_input(
        "A/B variant EMA Gap Floor %",
        min_value=0.0, max_value=1.0,
        value=float(os.getenv("EMA_GAP_FLOOR_VARIANT", "0.05")),
        step=0.01, format="%.2f",
        help="Second paper session runs with this gap_floor for A/B testing. "
             "Default reads EMA_GAP_FLOOR_VARIANT env var. Changing this only affects NEW variant sessions, "
             "not ones already running.",
    )
    capital = st.number_input("Capital (Rs.)", min_value=10000, max_value=10000000, value=100000, step=10000)

    st.markdown("---")
    st.subheader("Lot Size")
    lot_multipliers = {}
    for inst_name, inst_ic in INSTRUMENTS.items():
        lot_multipliers[inst_name] = st.number_input(
            f"{inst_name} (1 lot = {inst_ic.lot_size})",
            min_value=1, max_value=50, value=1, step=1,
            key=f"lot_mult_{inst_name}",
        )

    st.markdown("---")
    # Status indicators
    paper_active = [name for name, s in st.session_state.paper_sessions.items() if s.is_running]
    live_active = [name for name, s in st.session_state.live_sessions.items() if s.is_running]
    if paper_active:
        st.success(f"Paper: {', '.join(paper_active)}")
    if live_active:
        st.error(f"LIVE: {', '.join(live_active)}")

sc = StrategyConfig(extra_entry_mode=extra_mode, ema_gap_min=gap_min,
                    max_hold_candles=max_hold, cooldown_candles=cooldown,
                    candle_interval=candle_interval,
                    max_capital_per_trade_pct=max_capital_pct / 100,
                    orb_filter=orb_filter,
                    ema_gap_floor=ema_gap_floor)


# ---------------------------------------------------------------------------
# Paper A/B variants
# ---------------------------------------------------------------------------
# Baseline ("" variant) uses whatever the sidebar gap_floor says.
# The A/B "variant" forces ema_gap_floor = sidebar variant_gap_floor value,
# regardless of what the baseline is running with.

PAPER_VARIANT_KEYS: list[str] = ["", "variant"]


def _variant_label(variant: str) -> str:
    if not variant:
        return "baseline"
    if variant == "variant":
        return f"variant: gap_floor={variant_gap_floor:.2f}%"
    return variant


def _sc_for_variant(variant: str) -> StrategyConfig:
    """Return a StrategyConfig with per-variant overrides applied on top of sidebar sc."""
    if variant == "variant":
        return StrategyConfig(**{**sc.__dict__, "ema_gap_floor": variant_gap_floor})
    return sc


def _paper_session_key(inst_name: str, variant: str) -> str:
    return f"{inst_name}-{variant}" if variant else inst_name


def _get_ic(inst_name: str) -> InstrumentConfig:
    """Return InstrumentConfig with lot_size scaled by the sidebar multiplier."""
    base = INSTRUMENTS[inst_name]
    mult = lot_multipliers.get(inst_name, 1)
    if mult == 1:
        return base
    return InstrumentConfig(
        name=base.name, token=base.token, exchange=base.exchange,
        nfo_exchange=base.nfo_exchange, lot_size=base.lot_size * mult,
        strike_interval=base.strike_interval, expiry_weekday=base.expiry_weekday,
        expiry_type=base.expiry_type, expiry_flag=base.expiry_flag,
    )


ic = _get_ic(instrument)

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

tab_bt, tab_paper, tab_opt, tab_live = st.tabs(["Backtest", "Paper Trading", "Optimized Paper", "Live Trading"])

# ---------------------------------------------------------------------------
# Tab 1: Backtest
# ---------------------------------------------------------------------------

with tab_bt:
    st.header("Backtest")
    c1, c2 = st.columns(2)
    with c1:
        start_date = st.date_input("Start Date", value=date(2023, 4, 1))
    with c2:
        end_date = st.date_input("End Date", value=date(2026, 3, 28))

    # --- Dhan cache status & top-up ---
    spot_min, spot_max = _get_cached_date_range(
        instrument, "dhan_spot_candle",
        candle_type="intraday", interval_min=candle_interval,
    )
    opt1_min, opt1_max = _get_cached_date_range(
        instrument, "dhan_option_candle",
        strike_offset="ATM", direction="CALL",
        expiry_flag=_EXPIRY_FLAGS[instrument], expiry_code=1, interval_min=5,
    )
    opt2_min, opt2_max = _get_cached_date_range(
        instrument, "dhan_option_candle",
        strike_offset="ATM", direction="CALL",
        expiry_flag=_EXPIRY_FLAGS[instrument], expiry_code=2, interval_min=5,
    )

    cache_col1, cache_col2, cache_col3 = st.columns(3)
    cache_col1.caption(f"Spot {candle_interval}m: {spot_min or '—'} → {spot_max or '—'}")
    cache_col2.caption(f"Options code=1 (current week): {opt1_min or '—'} → {opt1_max or '—'}")
    cache_col3.caption(f"Options code=2 (next week): {opt2_min or '—'} → {opt2_max or '—'}")

    req_from = str(start_date)
    req_to = str(end_date)
    spot_gap = _resolve_incremental_range(spot_min, spot_max, req_from, req_to)
    opt1_gap = _resolve_incremental_range(opt1_min, opt1_max, req_from, req_to)
    opt2_gap = _resolve_incremental_range(opt2_min, opt2_max, req_from, req_to)

    if spot_gap is not None or opt1_gap is not None or opt2_gap is not None:
        msg_parts = []
        if spot_gap:
            msg_parts.append(f"spot {spot_gap[0]}..{spot_gap[1]}")
        if opt1_gap:
            msg_parts.append(f"options code=1 {opt1_gap[0]}..{opt1_gap[1]}")
        if opt2_gap:
            msg_parts.append(f"options code=2 {opt2_gap[0]}..{opt2_gap[1]}")
        st.warning("Cache is missing: " + "; ".join(msg_parts))

        if st.button("Fetch Missing Data from Dhan"):
            try:
                creds = init_dhan()
            except RuntimeError as e:
                st.error(str(e))
            else:
                progress = st.progress(0, text="Starting Dhan fetch…")
                if spot_gap is not None:
                    progress.progress(5, text=f"Fetching spot {spot_gap[0]}..{spot_gap[1]}")
                    fetch_index_intraday(
                        creds, instrument, candle_interval,
                        f"{spot_gap[0]} 09:15:00", f"{spot_gap[1]} 15:30:00",
                    )
                if opt1_gap is not None:
                    progress.progress(20, text=f"Fetching options code=1 {opt1_gap[0]}..{opt1_gap[1]} (30 streams)")
                    prefetch_option_data(
                        creds, instrument, _EXPIRY_FLAGS[instrument], 5,
                        opt1_gap[0], opt1_gap[1], expiry_codes=(1,),
                    )
                if opt2_gap is not None:
                    progress.progress(60, text=f"Fetching options code=2 {opt2_gap[0]}..{opt2_gap[1]} (30 streams)")
                    prefetch_option_data(
                        creds, instrument, _EXPIRY_FLAGS[instrument], 5,
                        opt2_gap[0], opt2_gap[1], expiry_codes=(2,),
                    )
                progress.progress(100, text="Done")
                st.success("Cache updated. Click Run Backtest.")
                st.rerun()
    else:
        st.success(f"Cache fully covers {req_from} → {req_to} (both expiry codes)")

    if st.button("Run Backtest", type="primary"):
        with st.spinner(f"Running backtest on {instrument}..."):
            trades = run_backtest(
                strat_config=sc, inst_config=ic,
                start_date=str(start_date), end_date=str(end_date),
            )
            stats = compute_stats(trades, capital)
            curve = compute_equity_curve(trades, capital)
            st.session_state.backtest_results = {"trades": trades, "stats": stats, "curve": curve}

    if st.session_state.backtest_results:
        r = st.session_state.backtest_results
        stats = r["stats"]
        if stats["n"] == 0:
            st.warning("No trades generated.")
        else:
            # Metric cards
            m1, m2, m3, m4, m5, m6 = st.columns(6)
            m1.metric("Trades", stats["n"])
            m2.metric("Win Rate", f"{stats['wr']:.0f}%")
            m3.metric("Net P&L", f"Rs.{stats['pnl']:,.0f}")
            m4.metric("Profit Factor", f"{stats['pf']:.2f}")
            m5.metric("Max DD", f"Rs.{stats['max_dd']:,.0f}")
            m6.metric("Return", f"{stats['return_pct']:.1f}%")

            # Equity curve
            curve = r["curve"]
            if len(curve) > 1:
                curve_df = pd.DataFrame(curve[1:], columns=["timestamp", "equity"])
                fig = go.Figure()
                fig.add_trace(go.Scatter(x=curve_df["timestamp"], y=curve_df["equity"],
                                         mode="lines", fill="tozeroy", name="Equity"))
                fig.update_layout(title="Equity Curve", xaxis_title="Date", yaxis_title="Equity (Rs.)",
                                  height=400)
                st.plotly_chart(fig, use_container_width=True)

            # Entry type breakdown
            if stats.get("entry_type_breakdown"):
                st.subheader("Entry Type Breakdown")
                et_rows = []
                for et, d in sorted(stats["entry_type_breakdown"].items()):
                    wr = d["w"] / d["n"] * 100 if d["n"] else 0
                    et_rows.append({"Type": et, "Trades": d["n"], "WR": f"{wr:.0f}%", "P&L": f"Rs.{d['pnl']:,.0f}"})
                st.dataframe(pd.DataFrame(et_rows), use_container_width=True, hide_index=True)

            # Exit breakdown
            st.subheader("Exit Breakdown")
            exit_rows = []
            for reason, d in sorted(stats["exit_breakdown"].items()):
                wr = d["w"] / d["n"] * 100 if d["n"] else 0
                exit_rows.append({"Reason": reason, "Trades": d["n"], "WR": f"{wr:.0f}%", "P&L": f"Rs.{d['pnl']:,.0f}"})
            st.dataframe(pd.DataFrame(exit_rows), use_container_width=True, hide_index=True)

            # Yearly
            st.subheader("Yearly Summary")
            yr_rows = []
            for y, d in sorted(stats["yearly"].items()):
                wr = d["w"] / d["n"] * 100 if d["n"] else 0
                yr_rows.append({"Year": y, "Trades": d["n"], "WR": f"{wr:.0f}%", "P&L": f"Rs.{d['pnl']:,.0f}"})
            st.dataframe(pd.DataFrame(yr_rows), use_container_width=True, hide_index=True)

            # Monthly
            with st.expander("Monthly P&L"):
                mo_rows = []
                for m, v in sorted(stats["monthly"].items()):
                    mo_rows.append({"Month": m, "P&L": f"Rs.{v:,.0f}", "Status": "+" if v >= 0 else "-"})
                st.dataframe(pd.DataFrame(mo_rows), use_container_width=True, hide_index=True)

            # Trade log
            with st.expander("Trade Log"):
                trade_df = pd.DataFrame(r["trades"])
                if not trade_df.empty:
                    display_cols = ["entry", "exit", "dir", "spot_in", "strike", "prem_in", "prem_out", "prem_pnl", "reason", "entry_type", "entry_gap"]
                    available = [c for c in display_cols if c in trade_df.columns]
                    st.dataframe(trade_df[available], use_container_width=True, hide_index=True)

            # Download report
            st.markdown("---")
            html_report = _generate_html_report(stats, curve, r["trades"], sc, ic, capital, start_date, end_date)
            filename = f"backtest_{ic.name}_{start_date}_{end_date}.html"
            st.download_button(
                label="Download Report (HTML)",
                data=html_report,
                file_name=filename,
                mime="text/html",
            )


# ---------------------------------------------------------------------------
# Tab 2: Paper Trading
# ---------------------------------------------------------------------------

def _render_trading_panel(session: TradingSession | None, label: str, store: TradeStore | None = None) -> None:
    """Render metrics and trade log for a running or completed session."""
    if session is None:
        # Show historical data from store even when no session is running
        if store:
            historical = store.load_trades()
            saved_equity = store.load_equity()
            if historical:
                st.subheader("Historical Trades")
                m1, m2 = st.columns(2)
                m1.metric("Past Trades", len(historical))
                if saved_equity is not None:
                    m2.metric("Last Equity", f"Rs.{saved_equity:,.0f}")
                with st.expander(f"Trade History ({len(historical)} trades)"):
                    trade_df = pd.DataFrame(historical)
                    if not trade_df.empty:
                        display_cols = ["entry", "exit", "dir", "strike", "prem_in", "prem_out", "prem_pnl", "reason"]
                        available = [c for c in display_cols if c in trade_df.columns]
                        st.dataframe(trade_df[available], use_container_width=True, hide_index=True)
            else:
                st.info(f"No {label} session running.")
        else:
            st.info(f"No {label} session running.")
        return

    status = session.get_status()
    if status.error:
        st.error(f"Session error:\n```\n{status.error}\n```")

    # Metrics
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Status", "RUNNING" if status.is_running else "STOPPED")
    m2.metric("Equity", f"Rs.{status.current_equity:,.0f}")
    pnl = status.current_equity - status.initial_capital
    m3.metric("P&L", f"Rs.{pnl:,.0f}")
    m4.metric("Trades", status.trade_count)

    # Open position
    if status.open_trade:
        st.warning(f"Open: {status.open_trade['dir']} {status.open_trade.get('symbol', '')} @ Rs.{status.open_trade.get('entry_price', 0):.1f}")

    # Recent trades
    if status.recent_trades:
        st.subheader("Recent Trades")
        st.dataframe(pd.DataFrame(status.recent_trades), use_container_width=True, hide_index=True)

    # All-time trade history from store
    if store:
        all_trades = store.load_trades()
        if len(all_trades) > len(status.recent_trades):
            with st.expander(f"All-Time History ({len(all_trades)} trades)"):
                trade_df = pd.DataFrame(all_trades)
                if not trade_df.empty:
                    display_cols = ["entry", "exit", "dir", "strike", "prem_in", "prem_out", "prem_pnl", "reason"]
                    available = [c for c in display_cols if c in trade_df.columns]
                    st.dataframe(trade_df[available], use_container_width=True, hide_index=True)


with tab_paper:
    st.header("Paper Trading")

    ALL_INSTRUMENTS = ["NIFTY", "BANKNIFTY", "SENSEX"]

    # Start/Stop all buttons
    all_paper_running = all(
        inst_name in st.session_state.paper_sessions and st.session_state.paper_sessions[inst_name].is_running
        for inst_name in ALL_INSTRUMENTS
    )
    any_paper_running = any(
        s.is_running for s in st.session_state.paper_sessions.values()
    )
    c1, c2, c3 = st.columns(3)
    with c1:
        if st.button("Start All Paper", type="primary", disabled=all_paper_running):
            for inst_name in ALL_INSTRUMENTS:
                if inst_name not in st.session_state.paper_sessions or not st.session_state.paper_sessions[inst_name].is_running:
                    old = st.session_state.paper_sessions.get(inst_name)
                    if old is not None:
                        old.stop()
                    inst_ic = _get_ic(inst_name)
                    session = TradingSession(sc, inst_ic, capital, live=False)
                    session.start()
                    st.session_state.paper_sessions[inst_name] = session
            st.rerun()
    with c2:
        if st.button("Stop All Paper", disabled=not any_paper_running):
            for session in st.session_state.paper_sessions.values():
                session.stop()
            st.rerun()

    st.markdown("---")

    # Per-instrument panels (baseline + variants)
    for inst_name in ALL_INSTRUMENTS:
        inst_ic = _get_ic(inst_name)
        base_session = st.session_state.paper_sessions.get(inst_name)
        any_variant_running = any(
            s.is_running for k, s in st.session_state.paper_sessions.items()
            if k == inst_name or k.startswith(f"{inst_name}-")
        )

        with st.expander(f"{inst_name} (Lot: {inst_ic.lot_size})", expanded=any_variant_running):
            for variant in PAPER_VARIANT_KEYS:
                variant_label = _variant_label(variant)
                session_key = _paper_session_key(inst_name, variant)
                store_key = session_key
                session = st.session_state.paper_sessions.get(session_key)
                variant_store = TradeStore(store_key, "paper", get_data_dir())

                if variant:
                    st.markdown("---")
                st.markdown(f"**{variant_label}**")

                # Recovery warning
                if session is None or not session.is_running:
                    open_trade = variant_store.load_open_trade()
                    if open_trade:
                        st.warning(
                            f"Unclosed position from previous session: "
                            f"{open_trade.get('dir')} {open_trade.get('symbol', '?')} "
                            f"@ Rs.{open_trade.get('entry_price', 0):.1f}. "
                            f"Start the session to auto-recover."
                        )

                col1, col2 = st.columns([3, 1])
                with col2:
                    running = session is not None and session.is_running
                    start_btn_label = f"Start {inst_name}" if not variant else f"Start {variant}"
                    stop_btn_label = f"Stop {inst_name}" if not variant else f"Stop {variant}"
                    if st.button(start_btn_label, key=f"start_paper_{session_key}", disabled=running):
                        old = st.session_state.paper_sessions.get(session_key)
                        if old is not None:
                            old.stop()
                        s = TradingSession(
                            _sc_for_variant(variant), _get_ic(inst_name), capital,
                            live=False, variant=variant,
                        )
                        s.start()
                        st.session_state.paper_sessions[session_key] = s
                        st.rerun()
                    if st.button(stop_btn_label, key=f"stop_paper_{session_key}", disabled=not running):
                        if session:
                            session.stop()
                            st.rerun()
                with col1:
                    panel_label = f"paper {inst_name}" if not variant else f"paper {inst_name} [{variant}]"
                    _render_trading_panel(session, panel_label, store=variant_store)


# ---------------------------------------------------------------------------
# Tab 3: Optimized Paper Trading (NIFTY intraday)
# ---------------------------------------------------------------------------

with tab_opt:
    st.header("Optimized Paper Trading")
    st.markdown(
        "**Config:** EMA 5/13 | ST 10/2.0 | gap_floor=0.10 | max_hold=15 | "
        "cooldown=2 | gap_min=0 | ORB 45min | Intraday"
    )
    st.caption("Research-optimized params: +46% P&L, +35% PF, +6% WR vs current system (backtest 2023-2026)")

    # Build the optimized StrategyConfig (hardcoded — isolated from sidebar)
    opt_sc = StrategyConfig(
        extra_entry_mode="midtrend",
        ema_gap_min=0,
        max_hold_candles=15,
        cooldown_candles=2,
        candle_interval=15,
        max_capital_per_trade_pct=0.25,
        orb_filter=True,
        ema_gap_floor=0.10,
        ema_short=5,
        ema_long=13,
        st_period=10,
        st_multiplier=2.0,
        orb_window_minutes=45,
    )

    OPT_INST = "NIFTY"
    opt_ic = _get_ic(OPT_INST)
    opt_session = st.session_state.opt_sessions.get(OPT_INST)
    opt_store = TradeStore(f"{OPT_INST}-optimized", "paper", get_data_dir())
    opt_running = opt_session is not None and opt_session.is_running

    # Recovery warning
    if not opt_running:
        open_trade = opt_store.load_open_trade()
        if open_trade:
            st.warning(
                f"Unclosed position from previous session: "
                f"{open_trade.get('dir')} {open_trade.get('symbol', '?')} "
                f"@ Rs.{open_trade.get('entry_price', 0):.1f}. "
                f"Start the session to auto-recover."
            )

    col1, col2 = st.columns([3, 1])
    with col2:
        if st.button("Start NIFTY Optimized", key="start_opt_nifty", type="primary", disabled=opt_running):
            old = st.session_state.opt_sessions.get(OPT_INST)
            if old is not None:
                old.stop()
            s = TradingSession(opt_sc, opt_ic, capital, live=False, variant="optimized")
            s.start()
            st.session_state.opt_sessions[OPT_INST] = s
            st.rerun()
        if st.button("Stop NIFTY Optimized", key="stop_opt_nifty", disabled=not opt_running):
            if opt_session:
                opt_session.stop()
                st.rerun()
    with col1:
        _render_trading_panel(opt_session, f"optimized paper {OPT_INST}", store=opt_store)


# ---------------------------------------------------------------------------
# Tab 4: Live Trading
# ---------------------------------------------------------------------------

with tab_live:
    st.header("Live Trading")
    st.error("Real money will be used. Proceed with extreme caution.")

    if not st.session_state.live_confirmed:
        st.markdown("### Safety Confirmation")
        chk1 = st.checkbox("I understand this will place REAL orders on Angel One")
        chk2 = st.checkbox("I have verified my API credentials and capital settings")
        chk3 = st.checkbox("I accept full responsibility for any losses")
        confirm_text = st.text_input("Type CONFIRM to enable live trading")

        if chk1 and chk2 and chk3 and confirm_text == "CONFIRM":
            if st.button("Enable Live Trading"):
                st.session_state.live_confirmed = True
                st.rerun()
    else:
        # Start/Stop all buttons
        all_live_running = all(
            inst_name in st.session_state.live_sessions and st.session_state.live_sessions[inst_name].is_running
            for inst_name in ALL_INSTRUMENTS
        )
        any_live_running = any(
            s.is_running for s in st.session_state.live_sessions.values()
        )
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("Start All Live", type="primary", disabled=all_live_running):
                for inst_name in ALL_INSTRUMENTS:
                    if inst_name not in st.session_state.live_sessions or not st.session_state.live_sessions[inst_name].is_running:
                        old = st.session_state.live_sessions.get(inst_name)
                        if old is not None:
                            old.stop()
                        inst_ic = _get_ic(inst_name)
                        session = TradingSession(sc, inst_ic, capital, live=True)
                        session.start()
                        st.session_state.live_sessions[inst_name] = session
                st.rerun()
        with c2:
            if st.button("Stop All Live", disabled=not any_live_running):
                for session in st.session_state.live_sessions.values():
                    session.stop()
                st.rerun()
        with c3:
            if st.button("Revoke Live Access"):
                st.session_state.live_confirmed = False
                for session in st.session_state.live_sessions.values():
                    session.stop()
                st.rerun()

        st.markdown("---")

        # Per-instrument panels
        for inst_name in ALL_INSTRUMENTS:
            inst_ic = _get_ic(inst_name)
            session = st.session_state.live_sessions.get(inst_name)
            live_store = TradeStore(inst_name, "live", get_data_dir())

            with st.expander(f"{inst_name} (Lot: {inst_ic.lot_size})", expanded=session is not None and session.is_running):
                # Recovery warning
                if session is None or not session.is_running:
                    open_trade = live_store.load_open_trade()
                    if open_trade:
                        st.error(
                            f"UNCLOSED LIVE POSITION from previous session: "
                            f"{open_trade.get('dir')} {open_trade.get('symbol', '?')} "
                            f"@ Rs.{open_trade.get('entry_price', 0):.1f}. "
                            f"Start the session IMMEDIATELY to auto-recover!"
                        )

                col1, col2 = st.columns([3, 1])
                with col2:
                    live_inst_running = session is not None and session.is_running
                    if st.button(f"Start {inst_name}", key=f"start_live_{inst_name}", disabled=live_inst_running):
                        old = st.session_state.live_sessions.get(inst_name)
                        if old is not None:
                            old.stop()
                        s = TradingSession(sc, _get_ic(inst_name), capital, live=True)
                        s.start()
                        st.session_state.live_sessions[inst_name] = s
                        st.rerun()
                    if st.button(f"Stop {inst_name}", key=f"stop_live_{inst_name}", disabled=not live_inst_running):
                        if session:
                            session.stop()
                            st.rerun()
                with col1:
                    _render_trading_panel(session, f"live {inst_name}", store=live_store)

# ---------------------------------------------------------------------------
# Auto-refresh while trading
# ---------------------------------------------------------------------------

any_running = (
    any(s.is_running for s in st.session_state.paper_sessions.values())
    or any(s.is_running for s in st.session_state.opt_sessions.values())
    or any(s.is_running for s in st.session_state.live_sessions.values())
)
if any_running:
    time.sleep(3)
    st.rerun()
