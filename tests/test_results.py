"""Tests for results.py — compute_stats and compute_equity_curve."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from results import compute_stats, compute_equity_curve


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_trade(
    pnl: float | None,
    *,
    direction: str = "CE",
    reason: str = "ST_flip",
    entry_type: str = "crossover",
    entry: datetime | None = None,
    exit: datetime | None = None,
) -> dict:
    """Build a minimal trade dict with sensible defaults."""
    entry = entry or datetime(2024, 1, 2, 10, 0)
    exit = exit or entry + timedelta(hours=1)
    return {
        "entry": entry,
        "exit": exit,
        "dir": direction,
        "prem_pnl": pnl,
        "reason": reason,
        "entry_type": entry_type,
    }


# ---------------------------------------------------------------------------
# TestComputeStats
# ---------------------------------------------------------------------------

class TestComputeStats:
    def test_empty_trades(self):
        assert compute_stats([]) == {"n": 0}

    def test_all_none_pnl(self):
        trades = [_make_trade(None), _make_trade(None)]
        assert compute_stats(trades) == {"n": 0}

    def test_single_winner(self, sample_trade):
        stats = compute_stats([sample_trade])
        assert stats["n"] == 1
        assert stats["winners"] == 1
        assert stats["wr"] == 100.0

    def test_single_loser(self):
        trade = _make_trade(-500.0)
        stats = compute_stats([trade])
        assert stats["n"] == 1
        assert stats["winners"] == 0
        assert stats["wr"] == 0.0

    def test_mixed_trades(self, sample_trades):
        trades = sample_trades(n=10, seed=42)
        stats = compute_stats(trades)
        assert stats["n"] == 10
        assert "wr" in stats
        assert "pnl" in stats

    def test_profit_factor_no_losses(self):
        trades = [_make_trade(100.0), _make_trade(200.0)]
        stats = compute_stats(trades)
        assert stats["pf"] == float("inf")

    def test_max_drawdown(self):
        # Equity curve: 0 -> +100 -> -100 -> -200  =>  peak=100, trough=-200, dd=300
        trades = [
            _make_trade(100.0, entry=datetime(2024, 1, 2, 10, 0)),
            _make_trade(-200.0, entry=datetime(2024, 1, 2, 11, 0)),
            _make_trade(-100.0, entry=datetime(2024, 1, 2, 12, 0)),
        ]
        stats = compute_stats(trades)
        assert stats["max_dd"] == 300.0

    def test_max_loss_streak(self):
        trades = [
            _make_trade(100.0, entry=datetime(2024, 1, 2, 10, 0)),
            _make_trade(-50.0, entry=datetime(2024, 1, 2, 11, 0)),
            _make_trade(-50.0, entry=datetime(2024, 1, 2, 12, 0)),
            _make_trade(-50.0, entry=datetime(2024, 1, 2, 13, 0)),
            _make_trade(100.0, entry=datetime(2024, 1, 2, 14, 0)),
        ]
        stats = compute_stats(trades)
        assert stats["max_streak"] == 3

    def test_monthly_breakdown(self):
        trades = [
            _make_trade(100.0, entry=datetime(2024, 1, 5, 10, 0)),
            _make_trade(200.0, entry=datetime(2024, 1, 15, 10, 0)),
            _make_trade(-50.0, entry=datetime(2024, 2, 5, 10, 0)),
        ]
        stats = compute_stats(trades)
        assert "2024-01" in stats["monthly"]
        assert "2024-02" in stats["monthly"]
        assert stats["monthly"]["2024-01"] == 300.0
        assert stats["monthly"]["2024-02"] == -50.0
        assert stats["green_months"] == 1
        assert stats["red_months"] == 1

    def test_direction_breakdown(self):
        trades = [
            _make_trade(200.0, direction="CE", entry=datetime(2024, 1, 2, 10, 0)),
            _make_trade(-50.0, direction="PE", entry=datetime(2024, 1, 2, 11, 0)),
        ]
        stats = compute_stats(trades)
        assert "CE" in stats["direction"]
        assert "PE" in stats["direction"]
        assert stats["direction"]["CE"]["n"] == 1
        assert stats["direction"]["CE"]["pnl"] == 200.0
        assert stats["direction"]["PE"]["n"] == 1
        assert stats["direction"]["PE"]["pnl"] == -50.0

    def test_exit_breakdown(self):
        trades = [
            _make_trade(100.0, reason="ST_flip", entry=datetime(2024, 1, 2, 10, 0)),
            _make_trade(-50.0, reason="max_hold", entry=datetime(2024, 1, 2, 11, 0)),
            _make_trade(80.0, reason="ST_flip", entry=datetime(2024, 1, 2, 12, 0)),
        ]
        stats = compute_stats(trades)
        eb = stats["exit_breakdown"]
        assert "ST_flip" in eb
        assert "max_hold" in eb
        assert eb["ST_flip"]["n"] == 2
        assert eb["ST_flip"]["pnl"] == 180.0
        assert eb["max_hold"]["n"] == 1

    def test_return_pct(self):
        capital = 100_000
        trades = [_make_trade(5000.0)]
        stats = compute_stats(trades, capital=capital)
        assert stats["return_pct"] == pytest.approx(5000.0 / capital * 100)


# ---------------------------------------------------------------------------
# TestComputeEquityCurve
# ---------------------------------------------------------------------------

class TestComputeEquityCurve:
    def test_empty_trades(self):
        curve = compute_equity_curve([])
        assert curve == [(None, 100_000)]

    def test_basic_curve(self):
        trades = [
            _make_trade(500.0, exit=datetime(2024, 1, 2, 11, 0)),
            _make_trade(-200.0, exit=datetime(2024, 1, 2, 12, 0)),
        ]
        curve = compute_equity_curve(trades, capital=100_000)
        assert len(curve) == 3
        assert curve[0] == (None, 100_000)
        assert curve[1] == (datetime(2024, 1, 2, 11, 0), 100_500)
        assert curve[2] == (datetime(2024, 1, 2, 12, 0), 100_300)

    def test_none_pnl_skipped(self):
        trades = [
            _make_trade(500.0, exit=datetime(2024, 1, 2, 11, 0)),
            _make_trade(None, exit=datetime(2024, 1, 2, 12, 0)),
            _make_trade(300.0, exit=datetime(2024, 1, 2, 13, 0)),
        ]
        curve = compute_equity_curve(trades, capital=100_000)
        assert len(curve) == 3  # initial + 2 valid trades
        assert curve[0] == (None, 100_000)
        assert curve[1] == (datetime(2024, 1, 2, 11, 0), 100_500)
        assert curve[2] == (datetime(2024, 1, 2, 13, 0), 100_800)
