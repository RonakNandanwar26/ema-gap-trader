"""costs.py — realistic fill prices and transaction charges for option trades.

The stock backtester fills at the cached candle close (mid/LTP) and charges
nothing. Live trading crosses the bid-ask spread on market orders, slips ticks,
and pays brokerage + statutory charges. This module models all of that so the
backtest can be re-run with honest costs.

Charge rates default to Indian index-option costs (post Oct-2024 STT).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

TICK = 0.05  # minimum option price


@dataclass
class CostConfig:
    half_spread_pct: float = 0.01       # fraction of premium crossed per side
    half_spread_min: float = 0.50       # Rs. floor per side
    slippage: float = 0.05              # Rs. lost to market-order slippage per side
    brokerage_per_order: float = 20.0   # flat per order (Angel One)
    stt_sell_pct: float = 0.001         # 0.1% of sell premium value
    txn_charge_pct: float = 0.0003503   # NSE 0.03503% of premium value, both legs
    stamp_duty_buy_pct: float = 0.00003  # 0.003% of buy value
    sebi_pct: float = 0.000001          # Rs.10/crore on turnover
    gst_pct: float = 0.18               # on brokerage + txn + SEBI charges

    @classmethod
    def zero(cls) -> "CostConfig":
        """Frictionless config — reproduces the raw backtest exactly."""
        return cls(half_spread_pct=0.0, half_spread_min=0.0, slippage=0.0,
                   brokerage_per_order=0.0, stt_sell_pct=0.0, txn_charge_pct=0.0,
                   stamp_duty_buy_pct=0.0, sebi_pct=0.0, gst_pct=0.0)

    @classmethod
    def from_env(cls) -> "CostConfig":
        return cls(
            half_spread_pct=float(os.getenv("COST_HALF_SPREAD_PCT", "0.01")),
            half_spread_min=float(os.getenv("COST_HALF_SPREAD_MIN", "0.50")),
            slippage=float(os.getenv("COST_SLIPPAGE", "0.05")),
            brokerage_per_order=float(os.getenv("COST_BROKERAGE", "20")),
        )


def _half_spread(premium: float, cfg: CostConfig) -> float:
    return max(cfg.half_spread_min, premium * cfg.half_spread_pct)


def entry_fill(premium: float | None, cfg: CostConfig) -> float | None:
    """Buy at mid + half-spread + slippage."""
    if premium is None:
        return None
    return premium + _half_spread(premium, cfg) + cfg.slippage


def exit_fill(premium: float | None, cfg: CostConfig) -> float | None:
    """Sell at mid - half-spread - slippage, floored at one tick."""
    if premium is None:
        return None
    return max(TICK, premium - _half_spread(premium, cfg) - cfg.slippage)


def round_trip_charges(buy_value: float, sell_value: float, cfg: CostConfig) -> float:
    """Total brokerage + statutory charges for one buy + one sell (Rs. values = premium * qty)."""
    turnover = buy_value + sell_value
    brokerage = 2 * cfg.brokerage_per_order
    stt = cfg.stt_sell_pct * sell_value
    txn = cfg.txn_charge_pct * turnover
    stamp = cfg.stamp_duty_buy_pct * buy_value
    sebi = cfg.sebi_pct * turnover
    gst = cfg.gst_pct * (brokerage + txn + sebi)
    return brokerage + stt + txn + stamp + sebi + gst


def net_premium_pnl(prem_in: float | None, prem_out: float | None,
                    lot_size: int, cfg: CostConfig) -> float | None:
    """Net P&L for one long-option round trip at cached mid prices prem_in/prem_out."""
    buy = entry_fill(prem_in, cfg)
    sell = exit_fill(prem_out, cfg)
    if buy is None or sell is None:
        return None
    return (sell - buy) * lot_size - round_trip_charges(buy * lot_size, sell * lot_size, cfg)
