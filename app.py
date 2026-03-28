"""app.py — Streamlit dashboard: Backtest | Paper Trading | Live Trading."""

from __future__ import annotations

import copy
import logging
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

from backtester import run_backtest
from config import StrategyConfig, InstrumentConfig, INSTRUMENTS, get_dhan_db_path
from results import compute_stats, compute_equity_curve
from trader import Trader

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------

st.set_page_config(page_title="EMA Gap Trader", page_icon=":chart_with_upwards_trend:", layout="wide")

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
    def __init__(self, strat_config: StrategyConfig, inst_config: InstrumentConfig, capital: int, live: bool = False):
        self.sc = strat_config
        self.ic = inst_config
        self.capital = capital
        self.live = live
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
        self._trader = Trader(self.sc, self.ic, self.capital, live=self.live)
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
                return TradingStatus(is_running=self.is_running, current_equity=float(self.capital),
                                     initial_capital=float(self.capital), trade_count=0,
                                     open_trade=None, error=self._error)
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
    capital = st.number_input("Capital (Rs.)", min_value=10000, max_value=10000000, value=100000, step=10000)

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
                    candle_interval=candle_interval)
ic = INSTRUMENTS[instrument]

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

tab_bt, tab_paper, tab_live = st.tabs(["Backtest", "Paper Trading", "Live Trading"])

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


# ---------------------------------------------------------------------------
# Tab 2: Paper Trading
# ---------------------------------------------------------------------------

def _render_trading_panel(session: TradingSession | None, label: str) -> None:
    """Render metrics and trade log for a running or completed session."""
    if session is None:
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


with tab_paper:
    st.header("Paper Trading")

    ALL_INSTRUMENTS = ["NIFTY", "BANKNIFTY", "SENSEX"]

    # Start/Stop all buttons
    c1, c2, c3 = st.columns(3)
    with c1:
        if st.button("Start All Paper", type="primary"):
            for inst_name in ALL_INSTRUMENTS:
                if inst_name not in st.session_state.paper_sessions or not st.session_state.paper_sessions[inst_name].is_running:
                    inst_ic = INSTRUMENTS[inst_name]
                    session = TradingSession(sc, inst_ic, capital, live=False)
                    session.start()
                    st.session_state.paper_sessions[inst_name] = session
            st.rerun()
    with c2:
        if st.button("Stop All Paper"):
            for session in st.session_state.paper_sessions.values():
                session.stop()
            st.rerun()

    st.markdown("---")

    # Per-instrument panels
    for inst_name in ALL_INSTRUMENTS:
        inst_ic = INSTRUMENTS[inst_name]
        session = st.session_state.paper_sessions.get(inst_name)

        with st.expander(f"{inst_name} (Lot: {inst_ic.lot_size})", expanded=session is not None and session.is_running):
            col1, col2 = st.columns([3, 1])
            with col2:
                if st.button(f"Start {inst_name}", key=f"start_paper_{inst_name}"):
                    s = TradingSession(sc, inst_ic, capital, live=False)
                    s.start()
                    st.session_state.paper_sessions[inst_name] = s
                    st.rerun()
                if st.button(f"Stop {inst_name}", key=f"stop_paper_{inst_name}"):
                    if session:
                        session.stop()
                        st.rerun()
            with col1:
                _render_trading_panel(session, f"paper {inst_name}")


# ---------------------------------------------------------------------------
# Tab 3: Live Trading
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
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("Start All Live", type="primary"):
                for inst_name in ALL_INSTRUMENTS:
                    if inst_name not in st.session_state.live_sessions or not st.session_state.live_sessions[inst_name].is_running:
                        inst_ic = INSTRUMENTS[inst_name]
                        session = TradingSession(sc, inst_ic, capital, live=True)
                        session.start()
                        st.session_state.live_sessions[inst_name] = session
                st.rerun()
        with c2:
            if st.button("Stop All Live"):
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
            inst_ic = INSTRUMENTS[inst_name]
            session = st.session_state.live_sessions.get(inst_name)

            with st.expander(f"{inst_name} (Lot: {inst_ic.lot_size})", expanded=session is not None and session.is_running):
                col1, col2 = st.columns([3, 1])
                with col2:
                    if st.button(f"Start {inst_name}", key=f"start_live_{inst_name}"):
                        s = TradingSession(sc, inst_ic, capital, live=True)
                        s.start()
                        st.session_state.live_sessions[inst_name] = s
                        st.rerun()
                    if st.button(f"Stop {inst_name}", key=f"stop_live_{inst_name}"):
                        if session:
                            session.stop()
                            st.rerun()
                with col1:
                    _render_trading_panel(session, f"live {inst_name}")

# ---------------------------------------------------------------------------
# Auto-refresh while trading
# ---------------------------------------------------------------------------

any_running = (
    any(s.is_running for s in st.session_state.paper_sessions.values())
    or any(s.is_running for s in st.session_state.live_sessions.values())
)
if any_running:
    time.sleep(3)
    st.rerun()
