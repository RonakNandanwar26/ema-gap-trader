"""Tests for the Dhan token file-based loader."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from dhan_data import _get_dhan_token


class TestGetDhanToken:
    """Tests for _get_dhan_token() — reads token from disk per call."""

    def test_reads_from_file_when_present(self, tmp_path: Path) -> None:
        token_file = tmp_path / "dhan.token"
        token_file.write_text("file-token-123\n")

        with patch("dhan_data._DHAN_TOKEN_PATH", str(token_file)):
            assert _get_dhan_token() == "file-token-123"

    def test_strips_whitespace_and_newlines(self, tmp_path: Path) -> None:
        token_file = tmp_path / "dhan.token"
        token_file.write_text("  spaced-token  \n\n")

        with patch("dhan_data._DHAN_TOKEN_PATH", str(token_file)):
            assert _get_dhan_token() == "spaced-token"

    def test_falls_back_to_env_var_when_file_missing(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist.token"

        with patch("dhan_data._DHAN_TOKEN_PATH", str(missing)), \
             patch.dict(os.environ, {"DHAN_ACCESS_TOKEN": "env-token-456"}, clear=False):
            assert _get_dhan_token() == "env-token-456"

    def test_returns_empty_string_when_neither_present(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist.token"

        with patch("dhan_data._DHAN_TOKEN_PATH", str(missing)), \
             patch.dict(os.environ, {}, clear=True):
            assert _get_dhan_token() == ""

    def test_reads_fresh_value_on_each_call(self, tmp_path: Path) -> None:
        """Verifies no caching — successive calls see file edits."""
        token_file = tmp_path / "dhan.token"
        token_file.write_text("first")

        with patch("dhan_data._DHAN_TOKEN_PATH", str(token_file)):
            assert _get_dhan_token() == "first"
            token_file.write_text("second")
            assert _get_dhan_token() == "second"
