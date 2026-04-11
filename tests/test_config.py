"""Tests for config.py — StrategyConfig, InstrumentConfig, and env-driven loaders."""

from __future__ import annotations

import os
from dataclasses import fields
from unittest.mock import patch

import pytest

from config import get_strategy_config, get_instrument_config, get_capital, INSTRUMENTS


# ---------------------------------------------------------------------------
# StrategyConfig
# ---------------------------------------------------------------------------
class TestGetStrategyConfig:
    def test_defaults(self):
        """No env vars set -> all defaults applied."""
        with patch.dict(os.environ, {}, clear=True):
            cfg = get_strategy_config()
        assert cfg.extra_entry_mode == "midtrend"
        assert cfg.ema_gap_min == 0.03
        assert cfg.max_hold_candles == 20
        assert cfg.cooldown_candles == 3
        assert cfg.candle_interval == 15
        assert cfg.max_capital_per_trade_pct == 0.25
        assert cfg.orb_filter is True
        assert cfg.ema_gap_floor == 0.0

    def test_from_env(self):
        """All env vars set -> custom values returned."""
        env = {
            "EXTRA_ENTRY_MODE": "expanding",
            "EMA_GAP_MIN": "0.05",
            "MAX_HOLD_CANDLES": "10",
            "COOLDOWN_CANDLES": "5",
            "CANDLE_INTERVAL": "5",
            "MAX_CAPITAL_PER_TRADE_PCT": "0.50",
            "ORB_FILTER": "false",
            "EMA_GAP_FLOOR": "0.07",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = get_strategy_config()
        assert cfg.extra_entry_mode == "expanding"
        assert cfg.ema_gap_min == 0.05
        assert cfg.max_hold_candles == 10
        assert cfg.cooldown_candles == 5
        assert cfg.candle_interval == 5
        assert cfg.max_capital_per_trade_pct == 0.50
        assert cfg.orb_filter is False
        assert cfg.ema_gap_floor == 0.07

    def test_orb_filter_true(self):
        """ORB_FILTER='true' -> True."""
        with patch.dict(os.environ, {"ORB_FILTER": "true"}, clear=True):
            cfg = get_strategy_config()
        assert cfg.orb_filter is True

    def test_orb_filter_false(self):
        """ORB_FILTER='false' -> False."""
        with patch.dict(os.environ, {"ORB_FILTER": "false"}, clear=True):
            cfg = get_strategy_config()
        assert cfg.orb_filter is False


# ---------------------------------------------------------------------------
# InstrumentConfig
# ---------------------------------------------------------------------------
class TestGetInstrumentConfig:
    def test_nifty(self):
        """NIFTY config has correct fields."""
        cfg = get_instrument_config("NIFTY")
        assert cfg.name == "NIFTY"
        assert cfg.token == "99926000"
        assert cfg.exchange == "NSE"
        assert cfg.nfo_exchange == "NFO"
        assert cfg.lot_size == 65
        assert cfg.strike_interval == 50
        assert cfg.expiry_weekday == 1
        assert cfg.expiry_type == "weekly"
        assert cfg.expiry_flag == "WEEK"

    def test_banknifty(self):
        """BANKNIFTY config."""
        cfg = get_instrument_config("BANKNIFTY")
        assert cfg.name == "BANKNIFTY"
        assert cfg.token == "99926009"
        assert cfg.exchange == "NSE"
        assert cfg.nfo_exchange == "NFO"
        assert cfg.lot_size == 30
        assert cfg.strike_interval == 100
        assert cfg.expiry_type == "monthly"
        assert cfg.expiry_flag == "MONTH"

    def test_sensex(self):
        """SENSEX config uses BSE/BFO exchanges."""
        cfg = get_instrument_config("SENSEX")
        assert cfg.name == "SENSEX"
        assert cfg.token == "99919000"
        assert cfg.exchange == "BSE"
        assert cfg.nfo_exchange == "BFO"
        assert cfg.lot_size == 20
        assert cfg.strike_interval == 100
        assert cfg.expiry_type == "weekly"
        assert cfg.expiry_flag == "WEEK"

    def test_unknown_raises(self):
        """Unknown instrument name raises ValueError."""
        with pytest.raises(ValueError, match="Unknown instrument"):
            get_instrument_config("MIDCAP")

    def test_from_env(self):
        """INSTRUMENT env var selects the instrument."""
        with patch.dict(os.environ, {"INSTRUMENT": "SENSEX"}, clear=True):
            cfg = get_instrument_config()
        assert cfg.name == "SENSEX"
        assert cfg.exchange == "BSE"


# ---------------------------------------------------------------------------
# Capital
# ---------------------------------------------------------------------------
class TestGetCapital:
    def test_default(self):
        """No CAPITAL env var -> 100000."""
        with patch.dict(os.environ, {}, clear=True):
            assert get_capital() == 100000

    def test_from_env(self):
        """CAPITAL env var -> custom value."""
        with patch.dict(os.environ, {"CAPITAL": "250000"}, clear=True):
            assert get_capital() == 250000


# ---------------------------------------------------------------------------
# INSTRUMENTS dict
# ---------------------------------------------------------------------------
class TestInstruments:
    def test_all_have_required_fields(self):
        """Every instrument in INSTRUMENTS has all InstrumentConfig fields."""
        from config import InstrumentConfig

        required_fields = {f.name for f in fields(InstrumentConfig)}
        for name, inst in INSTRUMENTS.items():
            assert isinstance(inst, InstrumentConfig), f"{name} is not InstrumentConfig"
            actual_fields = {f.name for f in fields(inst)}
            assert actual_fields == required_fields, (
                f"{name} missing fields: {required_fields - actual_fields}"
            )
