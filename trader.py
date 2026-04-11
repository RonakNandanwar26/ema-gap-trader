"""trader.py — Paper + live trading engine with WebSocket real-time exit monitoring."""

from __future__ import annotations

import logging
import time as time_mod
from datetime import date, datetime, time, timedelta

import pandas as pd

import broker
from config import (
    MARKET_CLOSE, MARKET_OPEN, TRADING_START,
    WS_ENABLED, WS_EXIT_CHECK_INTERVAL,
    StrategyConfig, InstrumentConfig, get_instrument_config, get_strategy_config, get_capital,
)
from indicators import compute_indicators
from notifications import send_telegram
from persistence import TradeStore
from strategy import check_entry, check_exit
from tick_manager import TickManager, EXCHANGE_NSE_CM, EXCHANGE_NSE_FO, EXCHANGE_BSE_FO

logger = logging.getLogger(__name__)


class Trader:
    """Single-strategy trading engine. Set live=True for real order execution."""

    def __init__(
        self,
        strat_config: StrategyConfig | None = None,
        inst_config: InstrumentConfig | None = None,
        capital: int | None = None,
        live: bool = False,
        trade_store: TradeStore | None = None,
        label: str = "",
    ):
        self.sc = strat_config or get_strategy_config()
        self.ic = inst_config or get_instrument_config()
        self.initial_capital = capital or get_capital()
        self.current_equity = float(self.initial_capital)
        self.live = live
        self._store = trade_store
        self._label = label  # optional tag (e.g. variant name) for log / telegram disambiguation

        self.open_trade: dict | None = None
        self.trades: list[dict] = []
        self.running = False

        self._scrip_master: dict | None = None
        self._current_expiry: date | None = None
        self._last_exit_idx = -999
        self._ws_exited_this_candle = False
        self._orb_high: float | None = None
        self._orb_low: float | None = None
        self._tick_manager: TickManager | None = None
        self._last_df: pd.DataFrame | None = None  # cached candle DF for virtual close

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Main polling loop — runs until market close or stopped."""
        self.running = True
        logger.info("Starting %s trader for %s", "LIVE" if self.live else "PAPER", self.ic.name)

        # Login + scrip master
        logger.info("Logging in to SmartAPI...")
        broker.login()
        logger.info("Loading scrip master...")
        self._scrip_master = broker.load_scrip_master()
        today = date.today()
        self._current_expiry = broker.get_current_expiry(self._scrip_master, self.ic.name, today)
        logger.info("Expiry resolved: %s", self._current_expiry)

        # Recovery: restore state from previous session
        if self._store:
            saved_equity = self._store.load_equity()
            if saved_equity is not None:
                self.current_equity = saved_equity
                logger.info("Restored equity from previous session: %.0f", saved_equity)
            # Load historical trades for continuity
            self.trades = self._store.load_trades()
            recovered = self._store.load_open_trade()
            if recovered:
                self._handle_recovery(recovered, today)

        # Early holiday/weekend detection using 1-min candle
        self._wait_until(time(9, 17))
        if not self.running:
            return
        if not self._is_market_open_today(today):
            logger.info("Market closed today (%s) — holiday/weekend. Stopping.", today)
            send_telegram(f"Market closed today ({today}) — holiday/weekend. Trader stopped.")
            self.running = False
            return

        # Start WebSocket if enabled
        if WS_ENABLED:
            try:
                auth, feed, api_key, client = broker.get_feed_credentials()
                self._tick_manager = TickManager(auth, api_key, client, feed)
                self._tick_manager.start()
                # Subscribe to spot index for virtual close computation
                spot_exchange = EXCHANGE_NSE_CM if self.ic.exchange == "NSE" else 1
                self._tick_manager.subscribe(self.ic.token, spot_exchange)
                logger.info("WebSocket enabled — real-time exit monitoring active")
            except Exception:
                logger.exception("WebSocket setup failed — falling back to polling")
                self._tick_manager = None

        tag = f"[{self._label}] " if self._label else ""
        send_telegram(
            f"{tag}{'LIVE' if self.live else 'PAPER'} trader started\n"
            f"Instrument: {self.ic.name}\n"
            f"Mode: crossover + {self.sc.extra_entry_mode}\n"
            f"Gap min: {self.sc.ema_gap_min}%\n"
            f"Gap floor: {self.sc.ema_gap_floor}%\n"
            f"Expiry: {self._current_expiry}\n"
            f"WebSocket: {'ON' if self._tick_manager else 'OFF'}"
        )

        # Wait for market open
        self._wait_until(MARKET_OPEN)

        candle_count = 0
        while self.running:
            now = datetime.now()
            if now.time() >= MARKET_CLOSE:
                break

            if now.time() < TRADING_START:
                time_mod.sleep(10)
                continue

            # Fetch candles
            df = self._fetch_candles(today)
            if df is None or len(df) < 10:
                logger.debug("Not enough candles yet (%d), waiting...", len(df) if df is not None else 0)
                time_mod.sleep(30)
                continue

            # Safety net: verify candles are from today
            latest_ts = pd.Timestamp(df.iloc[-1]["timestamp"])
            if latest_ts.date() < today:
                logger.info("No candles from today — market likely closed. Stopping.")
                send_telegram(f"No fresh candles for {today}. Market appears closed. Stopping.")
                break

            df = compute_indicators(df)
            self._last_df = df  # cache for virtual close checks
            idx = len(df) - 1
            row = df.iloc[idx]
            prev = df.iloc[idx - 1] if idx > 0 else None
            candle_count += 1

            # Compute ORB range from first 30 min of today
            if self.sc.orb_filter and self._orb_high is None:
                today_df = df[df["timestamp"].dt.date == today]
                first_30 = today_df[today_df["timestamp"].dt.time <= time(9, 30)]
                if len(first_30) >= 2:
                    self._orb_high = float(first_30["high"].max())
                    self._orb_low = float(first_30["low"].min())
                    logger.info("ORB range set: high=%.1f low=%.1f", self._orb_high, self._orb_low)

            # Log candle state
            logger.info(
                "Candle #%d | %s | Close=%.1f | EMA9=%.1f EMA21=%.1f | Gap=%.3f%% | ST=%d | RSI=%.1f",
                candle_count, row["timestamp"],
                row["close"], row["ema9"], row["ema21"],
                row["ema_gap_pct"], row["st_dir"],
                row["rsi"] if not pd.isna(row["rsi"]) else 0,
            )

            # Exit check on completed candle
            if self.open_trade is not None:
                candles_held = candle_count - self.open_trade.get("candle_num", 0)
                reason = check_exit(row, self.open_trade["dir"], candles_held, self.sc.max_hold_candles, self.sc.ema_gap_floor)
                if reason:
                    logger.info("EXIT SIGNAL (candle): %s after %d candles", reason, candles_held)
                    self._process_exit(reason, row)
                else:
                    # Log position status
                    opt_ltp = None
                    if self._tick_manager:
                        opt_ltp = self._tick_manager.get_ltp(self.open_trade["token"])
                    if opt_ltp is None:
                        opt_ltp = broker.fetch_ltp(self.ic.nfo_exchange, self.open_trade["symbol"], self.open_trade["token"])
                    if opt_ltp and self.open_trade["entry_price"]:
                        pct = (opt_ltp - self.open_trade["entry_price"]) / self.open_trade["entry_price"] * 100
                        logger.info(
                            "POSITION: %s %s | Entry=%.1f | Now=%.1f | P&L=%.0f%% | Held=%d candles",
                            self.open_trade["dir"], self.open_trade["symbol"],
                            self.open_trade["entry_price"], opt_ltp, pct, candles_held,
                        )

            # Entry check
            if self.open_trade is None and prev is not None:
                direction = check_entry(
                    row, prev, self.sc.extra_entry_mode, self.sc.ema_gap_min,
                    self._last_exit_idx, candle_count, self.sc.cooldown_candles,
                    self._orb_high, self._orb_low, self.sc.orb_filter,
                )
                if direction:
                    logger.info("ENTRY SIGNAL: %s | Gap=%.3f%% | RSI=%.1f", direction, row["ema_gap_pct"], row["rsi"] if not pd.isna(row["rsi"]) else 0)
                    self._process_entry(direction, row, today, candle_count)

            # Sleep until next candle — with real-time exit monitoring
            self._sleep_with_exit_monitoring(candle_count)

            if self._ws_exited_this_candle:
                self._ws_exited_this_candle = False
                continue

        # End of day — carry forward open position
        if self.open_trade is not None:
            logger.info("Market close — carrying forward open position: %s %s",
                        self.open_trade["dir"], self.open_trade.get("symbol"))
            tag = f"[{self._label}] " if self._label else ""
            send_telegram(
                f"{tag}CARRY FORWARD\n"
                f"{self.open_trade['dir']} {self.open_trade.get('symbol', '')}\n"
                f"Entry: Rs.{self.open_trade.get('entry_price', 0):.1f}\n"
                f"Position will resume next trading day."
            )

        # Cleanup WebSocket
        if self._tick_manager:
            self._tick_manager.stop()

        self.running = False
        tag = f"[{self._label}] " if self._label else ""
        send_telegram(
            f"{tag}{'LIVE' if self.live else 'PAPER'} session ended\n"
            f"Trades: {len(self.trades)}\n"
            f"Equity: Rs.{self.current_equity:,.0f}"
        )
        logger.info("Trader stopped. %d trades, equity=%.0f", len(self.trades), self.current_equity)

    # ------------------------------------------------------------------
    # Entry
    # ------------------------------------------------------------------

    def _process_entry(self, direction: str, row: pd.Series, today: date, candle_num: int) -> None:
        spot = float(row["close"])
        strike_interval = self.ic.strike_interval
        atm_strike = round(spot / strike_interval) * strike_interval
        qty = self.ic.lot_size
        max_spend = self.current_equity * self.sc.max_capital_per_trade_pct

        # Try ATM, then ATM+1, ATM+2 to find affordable strike
        symbol, token, premium, strike = None, None, None, atm_strike
        for step in range(3):  # 0=ATM, 1=ATM+1, 2=ATM+2
            try:
                symbol, token = broker.resolve_option(
                    self._scrip_master, self.ic.name, strike, direction, self._current_expiry,
                )
            except ValueError:
                logger.warning("Could not resolve option at strike %d", strike)
                break
            premium = broker.fetch_ltp(self.ic.nfo_exchange, symbol, token)
            if premium is None or premium <= 0:
                logger.warning("No LTP for strike %d, stopping search", strike)
                premium = None
                break
            if premium * qty <= max_spend:
                break  # affordable
            logger.info("Strike %d premium Rs.%.1f × %d = Rs.%.0f exceeds budget Rs.%.0f, trying OTM",
                        strike, premium, qty, premium * qty, max_spend)
            strike += strike_interval if direction == "CE" else -strike_interval
            symbol, token, premium = None, None, None

        if premium is None or premium * qty > max_spend:
            logger.warning("No affordable strike (ATM to ATM+2), skipping. Budget=Rs.%.0f", max_spend)
            send_telegram(f"SKIP {direction}: no affordable strike within budget Rs.{max_spend:,.0f}")
            return

        if strike != atm_strike:
            logger.info("Moved to OTM strike %d (ATM was %d) to fit budget", strike, atm_strike)

        # Live: place real order
        if self.live:
            order_id = broker.place_order({
                "variety": "NORMAL", "tradingsymbol": symbol, "symboltoken": token,
                "transactiontype": "BUY", "exchange": self.ic.nfo_exchange,
                "ordertype": "MARKET", "producttype": "CARRYFORWARD", "duration": "DAY",
                "quantity": str(qty), "price": "0", "squareoff": "0", "stoploss": "0",
            })
            if order_id is None:
                logger.error("Order placement failed, skipping entry")
                return
            fill = broker.verify_order(order_id)
            if fill and fill.get("averageprice"):
                premium = float(fill["averageprice"])

        self.open_trade = {
            "dir": direction, "entry_time": datetime.now(), "entry_price": premium,
            "spot_entry": spot, "strike": strike, "quantity": qty,
            "symbol": symbol, "token": token, "candle_num": candle_num,
            "rsi": row.get("rsi"), "gap": row.get("ema_gap_pct"),
        }

        # Persist open trade to disk
        if self._store:
            self._store.save_open_trade(
                {**self.open_trade, "expiry": self._current_expiry},
                self.current_equity,
            )

        # Subscribe to option token for real-time premium tracking
        if self._tick_manager:
            opt_exchange = EXCHANGE_BSE_FO if self.ic.nfo_exchange == "BFO" else EXCHANGE_NSE_FO
            self._tick_manager.subscribe(token, opt_exchange)

        otm_note = f" (OTM from ATM {atm_strike})" if strike != atm_strike else ""
        tag = f"[{self._label}] " if self._label else ""
        msg = (
            f"{tag}ENTRY {'LIVE' if self.live else 'PAPER'}\n"
            f"{direction} {symbol} @ Rs.{premium:.1f}{otm_note}\n"
            f"Spot: {spot:.1f} | Strike: {strike} | Cost: Rs.{premium * qty:,.0f}\n"
            f"RSI: {row.get('rsi', 0):.1f} | Gap: {row.get('ema_gap_pct', 0):.3f}%"
        )
        send_telegram(msg)
        logger.info(msg.replace("\n", " | "))

    # ------------------------------------------------------------------
    # Exit
    # ------------------------------------------------------------------

    def _process_exit(self, reason: str, row: pd.Series | None) -> None:
        if self.open_trade is None:
            return

        trade = self.open_trade
        exit_premium = None

        # Try cached LTP first (faster), fallback to REST
        if self._tick_manager:
            exit_premium = self._tick_manager.get_ltp(trade["token"])
        if exit_premium is None:
            exit_premium = broker.fetch_ltp(self.ic.nfo_exchange, trade["symbol"], trade["token"])

        # Live: place sell order
        if self.live:
            order_id = broker.place_order({
                "variety": "NORMAL", "tradingsymbol": trade["symbol"],
                "symboltoken": trade["token"], "transactiontype": "SELL",
                "exchange": self.ic.nfo_exchange, "ordertype": "MARKET",
                "producttype": "CARRYFORWARD", "duration": "DAY",
                "quantity": str(trade["quantity"]), "price": "0",
                "squareoff": "0", "stoploss": "0",
            })
            if order_id:
                fill = broker.verify_order(order_id)
                if fill and fill.get("averageprice"):
                    exit_premium = float(fill["averageprice"])

        pnl = None
        if exit_premium and trade["entry_price"]:
            pnl = (exit_premium - trade["entry_price"]) * trade["quantity"]

        spot_out = float(row["close"]) if row is not None else None

        completed = {
            "entry": trade["entry_time"], "exit": datetime.now(),
            "dir": trade["dir"], "spot_in": trade["spot_entry"], "spot_out": spot_out,
            "strike": trade["strike"], "prem_in": trade["entry_price"],
            "prem_out": exit_premium, "prem_pnl": pnl,
            "reason": reason, "entry_rsi": trade.get("rsi"),
            "entry_gap": trade.get("gap"), "entry_type": "live",
        }
        self.trades.append(completed)

        if pnl is not None:
            self.current_equity += pnl

        # Persist trade and clear open position
        if self._store:
            self._store.append_trade(completed)
            self._store.clear_open_trade()
            self._store.save_equity(self.current_equity)

        # Unsubscribe option token
        if self._tick_manager:
            opt_exchange = EXCHANGE_BSE_FO if self.ic.nfo_exchange == "BFO" else EXCHANGE_NSE_FO
            self._tick_manager.unsubscribe(trade["token"], opt_exchange)

        self.open_trade = None
        self._last_exit_idx = trade.get("candle_num", 0)

        pnl_text = f"P&L: Rs.{pnl:,.0f}" if pnl else "P&L: N/A"
        tag = f"[{self._label}] " if self._label else ""
        msg = (
            f"{tag}EXIT {'LIVE' if self.live else 'PAPER'} — {reason}\n"
            f"{trade['dir']} {trade['symbol']}\n"
            f"Entry: Rs.{trade['entry_price']:.1f} → Exit: Rs.{(exit_premium if exit_premium else 0):.1f}\n"
            f"{pnl_text}"
        )
        send_telegram(msg)
        logger.info(msg.replace("\n", " | "))

    # ------------------------------------------------------------------
    # Real-time exit monitoring (WebSocket-powered)
    # ------------------------------------------------------------------

    def _check_virtual_exit(self, candle_count: int) -> str | None:
        """Check exit using live spot tick as virtual candle close.

        Takes the last completed candle DataFrame, appends a virtual row
        with close = current spot LTP, recomputes indicators, and checks
        if exit conditions are met.
        """
        if self._tick_manager is None or self._last_df is None or self.open_trade is None:
            return None

        # Get live spot price
        spot_ltp = self._tick_manager.get_ltp(self.ic.token)
        if spot_ltp is None:
            return None
        logger.debug("Virtual close check: spot=%.1f", spot_ltp)

        # Build virtual candle: copy last row, override close with live price
        df = self._last_df.copy()
        virtual_row = df.iloc[-1].copy()
        virtual_row["close"] = spot_ltp
        virtual_row["high"] = max(virtual_row["high"], spot_ltp)
        virtual_row["low"] = min(virtual_row["low"], spot_ltp)
        # Append as new row
        virtual_df = pd.concat([df, pd.DataFrame([virtual_row])], ignore_index=True)

        # Recompute indicators on extended DataFrame
        try:
            virtual_df = compute_indicators(virtual_df)
        except Exception:
            logger.debug("compute_indicators failed on virtual candle, skipping check")
            return None
        virtual_last = virtual_df.iloc[-1]

        # Check exit on virtual row
        candles_held = candle_count - self.open_trade.get("candle_num", 0)
        reason = check_exit(virtual_last, self.open_trade["dir"], candles_held, self.sc.max_hold_candles, self.sc.ema_gap_floor)
        return reason

    def _sleep_with_exit_monitoring(self, candle_count: int) -> None:
        """Sleep until next candle boundary, checking exits every N seconds via WebSocket."""
        now = datetime.now()
        # Next candle boundary + 15s buffer
        interval = self.sc.candle_interval
        minute = now.minute
        next_minute = ((minute // interval) + 1) * interval
        if next_minute >= 60:
            target = now.replace(hour=now.hour + 1, minute=0, second=15, microsecond=0)
        else:
            target = now.replace(minute=next_minute, second=15, microsecond=0)

        # If no open trade or no WebSocket, sleep in short chunks (responsive to stop)
        if self.open_trade is None or self._tick_manager is None:
            while datetime.now() < target and self.running:
                time_mod.sleep(2)
            return

        # Poll for exit every WS_EXIT_CHECK_INTERVAL seconds
        while datetime.now() < target and self.running:
            if self.open_trade is None:
                break  # already exited

            reason = self._check_virtual_exit(candle_count)
            if reason:
                logger.info("WebSocket exit triggered mid-candle: %s", reason)
                self._ws_exited_this_candle = True
                # Use virtual row = None, exit will fetch LTP from cache/REST
                self._process_exit(f"ws_{reason}", None)
                return

            time_mod.sleep(WS_EXIT_CHECK_INTERVAL)

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------

    def _fetch_candles(self, today: date) -> pd.DataFrame | None:
        """Fetch candles for the last 3 days + today."""
        from_date = (today - timedelta(days=3)).strftime("%Y-%m-%d")
        to_date = datetime.now().strftime("%Y-%m-%d %H:%M")
        try:
            return broker.fetch_candles(
                self.ic.token, self.ic.exchange, self.sc.candle_interval, from_date, to_date,
            )
        except Exception:
            logger.exception("Candle fetch failed")
            return None

    def _is_market_open_today(self, today: date) -> bool:
        """Fetch a 1-minute candle to check if the market is open today."""
        from_date = today.strftime("%Y-%m-%d")
        to_date = datetime.now().strftime("%Y-%m-%d %H:%M")
        try:
            df = broker.fetch_candles(self.ic.token, self.ic.exchange, 1, from_date, to_date)
            if df is None or df.empty:
                return False
            latest = pd.Timestamp(df.iloc[-1]["timestamp"]).date()
            return latest == today
        except Exception:
            logger.exception("Market open check failed")
            return False

    # ------------------------------------------------------------------
    # Timing helpers
    # ------------------------------------------------------------------

    def _wait_until(self, target: time) -> None:
        while self.running:
            now = datetime.now().time()
            if now >= target:
                return
            time_mod.sleep(2)  # short sleep so stop() is responsive

    # ------------------------------------------------------------------
    # Recovery from previous session
    # ------------------------------------------------------------------

    def _handle_recovery(self, recovered: dict, today: date) -> None:
        """Handle an open trade found from a previous crashed/stopped session."""
        expiry = recovered.get("expiry")
        symbol = recovered.get("symbol", "?")
        direction = recovered.get("dir", "?")

        logger.info("RECOVERY: found open trade — %s %s (expiry=%s)", direction, symbol, expiry)
        send_telegram(
            f"RECOVERY: open trade from previous session\n"
            f"{direction} {symbol}\n"
            f"Entry: Rs.{recovered.get('entry_price', 0):.1f}\n"
            f"Attempting recovery..."
        )

        # Scenario 1: contract has expired
        if expiry and expiry < today:
            logger.warning("Contract expired (%s < %s) — closing with loss", expiry, today)
            pnl = -(recovered.get("entry_price", 0) * recovered.get("quantity", 0))
            completed = {
                "entry": recovered.get("entry_time", datetime.now()),
                "exit": datetime.now(),
                "dir": direction,
                "spot_in": recovered.get("spot_entry"),
                "spot_out": None,
                "strike": recovered.get("strike"),
                "prem_in": recovered.get("entry_price"),
                "prem_out": 0,
                "prem_pnl": pnl,
                "reason": "recovery_expired",
                "entry_rsi": recovered.get("rsi"),
                "entry_gap": recovered.get("gap"),
                "entry_type": "live",
            }
            self.trades.append(completed)
            self.current_equity += pnl
            if self._store:
                self._store.append_trade(completed)
                self._store.clear_open_trade()
                self._store.save_equity(self.current_equity)
            send_telegram(
                f"RECOVERY CLOSED (expired)\n{direction} {symbol}\nP&L: Rs.{pnl:,.0f}"
            )
            return

        # Scenario 2 & 3: contract still valid — resume monitoring
        self.open_trade = {
            "dir": direction,
            "entry_time": recovered.get("entry_time", datetime.now()),
            "entry_price": recovered.get("entry_price"),
            "spot_entry": recovered.get("spot_entry"),
            "strike": recovered.get("strike"),
            "quantity": recovered.get("quantity"),
            "symbol": symbol,
            "token": recovered.get("token"),
            "candle_num": recovered.get("candle_num", 0),
            "rsi": recovered.get("rsi"),
            "gap": recovered.get("gap"),
        }
        logger.info("RECOVERY: position restored — will manage in normal loop")
        send_telegram(
            f"RECOVERY: position restored\n"
            f"{direction} {symbol} @ Rs.{recovered.get('entry_price', 0):.1f}\n"
            f"Will monitor for exit conditions."
        )
