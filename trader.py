"""trader.py — Paper + live trading engine with WebSocket real-time exit monitoring."""

from __future__ import annotations

import logging
import time as time_mod
from datetime import date, datetime, time, timedelta

import pandas as pd

import broker
from config import (
    MARKET_CLOSE, MARKET_OPEN, TIME_EXIT, TRADING_START,
    WS_ENABLED, WS_EXIT_CHECK_INTERVAL,
    StrategyConfig, InstrumentConfig, get_instrument_config, get_strategy_config, get_capital,
)
from indicators import compute_indicators
from notifications import send_telegram
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
    ):
        self.sc = strat_config or get_strategy_config()
        self.ic = inst_config or get_instrument_config()
        self.initial_capital = capital or get_capital()
        self.current_equity = float(self.initial_capital)
        self.live = live

        self.open_trade: dict | None = None
        self.trades: list[dict] = []
        self.running = False

        self._scrip_master: dict | None = None
        self._current_expiry: date | None = None
        self._last_exit_idx = -999
        self._tick_manager: TickManager | None = None
        self._last_df: pd.DataFrame | None = None  # cached candle DF for virtual close
        self._daily_pnl: float = 0.0
        self._daily_limit_hit: bool = False

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

        send_telegram(
            f"{'LIVE' if self.live else 'PAPER'} trader started\n"
            f"Instrument: {self.ic.name}\n"
            f"Mode: crossover + {self.sc.extra_entry_mode}\n"
            f"Gap min: {self.sc.ema_gap_min}%\n"
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

            # Force close at TIME_EXIT
            if now.time() >= TIME_EXIT and self.open_trade is not None:
                self._process_exit("time_exit", None)
                continue

            if now.time() < TRADING_START:
                time_mod.sleep(10)
                continue

            # Fetch candles
            df = self._fetch_candles(today)
            if df is None or len(df) < 3:
                logger.debug("Not enough candles yet (%d), waiting...", len(df) if df is not None else 0)
                time_mod.sleep(30)
                continue

            df = compute_indicators(df)
            self._last_df = df  # cache for virtual close checks
            idx = len(df) - 1
            row = df.iloc[idx]
            prev = df.iloc[idx - 1] if idx > 0 else None
            candle_count += 1

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
                reason = check_exit(row, self.open_trade["dir"], candles_held, self.sc.max_hold_candles)

                # Premium-based stop loss check (candle-level fallback)
                if reason is None and self.sc.stop_loss_pct > 0:
                    opt_ltp = None
                    if self._tick_manager:
                        opt_ltp = self._tick_manager.get_ltp(self.open_trade["token"])
                    if opt_ltp is None:
                        opt_ltp = broker.fetch_ltp(self.ic.nfo_exchange, self.open_trade["symbol"], self.open_trade["token"])
                    if opt_ltp is not None and self.open_trade["entry_price"] is not None:
                        drop_pct = (self.open_trade["entry_price"] - opt_ltp) / self.open_trade["entry_price"] * 100
                        if drop_pct >= self.sc.stop_loss_pct:
                            reason = "stop_loss"

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
            if self.open_trade is None and prev is not None and not self._daily_limit_hit:
                direction = check_entry(
                    row, prev, self.sc.extra_entry_mode, self.sc.ema_gap_min,
                    self._last_exit_idx, candle_count, self.sc.cooldown_candles,
                )
                if direction:
                    logger.info("ENTRY SIGNAL: %s | Gap=%.3f%% | RSI=%.1f", direction, row["ema_gap_pct"], row["rsi"] if not pd.isna(row["rsi"]) else 0)
                    self._process_entry(direction, row, today, candle_count)

            # Sleep until next candle — with real-time exit monitoring
            self._sleep_with_exit_monitoring(candle_count)

        # End of day
        if self.open_trade is not None:
            self._process_exit("market_close", None)

        # Cleanup WebSocket
        if self._tick_manager:
            self._tick_manager.stop()

        self.running = False
        send_telegram(
            f"{'LIVE' if self.live else 'PAPER'} session ended\n"
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

        # Resolve option contract
        try:
            symbol, token = broker.resolve_option(
                self._scrip_master, self.ic.name, atm_strike, direction, self._current_expiry,
            )
        except ValueError as e:
            logger.warning("Could not resolve option: %s", e)
            return

        # Get premium
        premium = broker.fetch_ltp(self.ic.nfo_exchange, symbol, token)
        if premium is None or premium <= 0:
            logger.warning("No LTP for %s, skipping entry", symbol)
            return

        qty = self.ic.lot_size

        # Live: place real order
        if self.live:
            order_id = broker.place_order({
                "variety": "NORMAL", "tradingsymbol": symbol, "symboltoken": token,
                "transactiontype": "BUY", "exchange": self.ic.nfo_exchange,
                "ordertype": "MARKET", "producttype": "INTRADAY", "duration": "DAY",
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
            "spot_entry": spot, "strike": atm_strike, "quantity": qty,
            "symbol": symbol, "token": token, "candle_num": candle_num,
            "rsi": row.get("rsi"), "gap": row.get("ema_gap_pct"),
        }

        # Subscribe to option token for real-time premium tracking
        if self._tick_manager:
            opt_exchange = EXCHANGE_BSE_FO if self.ic.nfo_exchange == "BFO" else EXCHANGE_NSE_FO
            self._tick_manager.subscribe(token, opt_exchange)

        msg = (
            f"ENTRY {'LIVE' if self.live else 'PAPER'}\n"
            f"{direction} {symbol} @ Rs.{premium:.1f}\n"
            f"Spot: {spot:.1f} | Strike: {atm_strike}\n"
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
                "producttype": "INTRADAY", "duration": "DAY",
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
            self._daily_pnl += pnl
            # Check daily loss limit
            if (self.sc.daily_loss_limit_pct > 0
                    and not self._daily_limit_hit
                    and self._daily_pnl <= -(self.sc.daily_loss_limit_pct / 100 * self.initial_capital)):
                self._daily_limit_hit = True
                logger.warning(
                    "DAILY LOSS LIMIT hit: daily P&L = Rs.%.0f (limit = Rs.%.0f)",
                    self._daily_pnl, -(self.sc.daily_loss_limit_pct / 100 * self.initial_capital),
                )
                send_telegram(
                    f"DAILY LOSS LIMIT HIT\n"
                    f"Daily P&L: Rs.{self._daily_pnl:,.0f}\n"
                    f"Limit: {self.sc.daily_loss_limit_pct}% of Rs.{self.initial_capital:,.0f}\n"
                    f"No more entries today."
                )

        # Unsubscribe option token
        if self._tick_manager:
            opt_exchange = EXCHANGE_BSE_FO if self.ic.nfo_exchange == "BFO" else EXCHANGE_NSE_FO
            self._tick_manager.unsubscribe(trade["token"], opt_exchange)

        self.open_trade = None
        self._last_exit_idx = trade.get("candle_num", 0)

        pnl_str = f"Rs.{pnl:,.0f}" if pnl is not None else "N/A"
        msg = (
            f"EXIT {'LIVE' if self.live else 'PAPER'} — {reason}\n"
            f"{trade['dir']} {trade['symbol']}\n"
            f"Entry: Rs.{trade['entry_price']:.1f} → Exit: Rs.{exit_premium:.1f if exit_premium else 0}\n"
            f"P&L: {pnl_str}"
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
        virtual_df = compute_indicators(virtual_df)
        virtual_last = virtual_df.iloc[-1]

        # Check exit on virtual row
        candles_held = candle_count - self.open_trade.get("candle_num", 0)
        reason = check_exit(virtual_last, self.open_trade["dir"], candles_held, self.sc.max_hold_candles)
        return reason

    def _sleep_with_exit_monitoring(self, candle_count: int) -> None:
        """Sleep until next candle boundary, checking exits every N seconds via WebSocket."""
        now = datetime.now()
        # Next 5-minute boundary + 15s buffer
        minute = now.minute
        next_minute = ((minute // 5) + 1) * 5
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
            # Time exit check
            if datetime.now().time() >= TIME_EXIT:
                self._process_exit("time_exit", None)
                return

            if self.open_trade is None:
                break  # already exited

            # Premium-based stop loss check (real-time via WebSocket)
            if self.sc.stop_loss_pct > 0:
                opt_ltp = self._tick_manager.get_ltp(self.open_trade["token"])
                if opt_ltp is not None and self.open_trade["entry_price"] is not None:
                    drop_pct = (self.open_trade["entry_price"] - opt_ltp) / self.open_trade["entry_price"] * 100
                    if drop_pct >= self.sc.stop_loss_pct:
                        logger.info(
                            "STOP LOSS triggered (WS): premium %.1f -> %.1f (%.1f%% drop)",
                            self.open_trade["entry_price"], opt_ltp, drop_pct,
                        )
                        self._process_exit("stop_loss", None)
                        return

            reason = self._check_virtual_exit(candle_count)
            if reason:
                logger.info("WebSocket exit triggered mid-candle: %s", reason)
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
                self.ic.token, self.ic.exchange, 5, from_date, to_date,
            )
        except Exception:
            logger.exception("Candle fetch failed")
            return None

    # ------------------------------------------------------------------
    # Timing helpers
    # ------------------------------------------------------------------

    def _wait_until(self, target: time) -> None:
        while self.running:
            now = datetime.now().time()
            if now >= target:
                return
            time_mod.sleep(2)  # short sleep so stop() is responsive
