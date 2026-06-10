"""Tests for costs.py — realistic fill prices and transaction charges."""

from __future__ import annotations

import pytest

from costs import CostConfig, entry_fill, exit_fill, round_trip_charges, net_premium_pnl


@pytest.fixture
def cfg():
    return CostConfig(
        half_spread_pct=0.01,      # 1% of premium crossed per side
        half_spread_min=0.50,      # but never less than Rs.0.50 per side
        slippage=0.05,             # extra ticks lost on market orders
        brokerage_per_order=20.0,
        stt_sell_pct=0.001,        # 0.1% of sell premium value
        txn_charge_pct=0.0003503,  # NSE 0.03503% of premium value, both legs
        stamp_duty_buy_pct=0.00003,
        sebi_pct=0.000001,
        gst_pct=0.18,
    )


# ---------------------------------------------------------------------------
# Fill prices
# ---------------------------------------------------------------------------
class TestFills:
    def test_entry_fill_adds_pct_half_spread_and_slippage(self, cfg):
        # Rs.100 premium: 1% half-spread = 1.00 > min 0.50, so 100 + 1.00 + 0.05
        assert entry_fill(100.0, cfg) == pytest.approx(101.05)

    def test_entry_fill_uses_min_half_spread_on_cheap_premium(self, cfg):
        # Rs.30 premium: 1% = 0.30 < min 0.50, so 30 + 0.50 + 0.05
        assert entry_fill(30.0, cfg) == pytest.approx(30.55)

    def test_exit_fill_subtracts_half_spread_and_slippage(self, cfg):
        assert exit_fill(100.0, cfg) == pytest.approx(98.95)

    def test_exit_fill_floors_at_tick(self, cfg):
        # Premium near zero can't fill negative — floor at Rs.0.05
        assert exit_fill(0.30, cfg) == pytest.approx(0.05)

    def test_none_passthrough(self, cfg):
        assert entry_fill(None, cfg) is None
        assert exit_fill(None, cfg) is None


# ---------------------------------------------------------------------------
# Charges
# ---------------------------------------------------------------------------
class TestCharges:
    def test_round_trip_charges_composition(self, cfg):
        buy_value, sell_value = 6500.0, 7800.0  # premium * lot
        brokerage = 40.0
        stt = 0.001 * sell_value
        txn = 0.0003503 * (buy_value + sell_value)
        stamp = 0.00003 * buy_value
        sebi = 0.000001 * (buy_value + sell_value)
        gst = 0.18 * (brokerage + txn + sebi)
        expected = brokerage + stt + txn + stamp + sebi + gst
        assert round_trip_charges(buy_value, sell_value, cfg) == pytest.approx(expected)

    def test_charges_scale_with_turnover(self, cfg):
        small = round_trip_charges(1000.0, 1000.0, cfg)
        large = round_trip_charges(100000.0, 100000.0, cfg)
        assert large > small > 40.0  # brokerage floor


# ---------------------------------------------------------------------------
# Net P&L
# ---------------------------------------------------------------------------
class TestNetPnl:
    def test_net_pnl_below_gross(self, cfg):
        gross = (50.0 - 40.0) * 65
        net = net_premium_pnl(prem_in=40.0, prem_out=50.0, lot_size=65, cfg=cfg)
        assert net < gross

    def test_net_pnl_matches_manual_computation(self, cfg):
        buy = entry_fill(40.0, cfg)            # 40.55
        sell = exit_fill(50.0, cfg)            # 49.45
        charges = round_trip_charges(buy * 65, sell * 65, cfg)
        expected = (sell - buy) * 65 - charges
        assert net_premium_pnl(40.0, 50.0, 65, cfg) == pytest.approx(expected)

    def test_net_pnl_none_when_premium_missing(self, cfg):
        assert net_premium_pnl(None, 50.0, 65, cfg) is None
        assert net_premium_pnl(40.0, None, 65, cfg) is None

    def test_zero_cost_config_reproduces_gross(self):
        zero = CostConfig.zero()
        assert net_premium_pnl(40.0, 50.0, 65, zero) == pytest.approx((50.0 - 40.0) * 65)
