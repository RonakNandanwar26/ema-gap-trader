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


class TestDhanCallsUseFreshToken:
    """Verifies the Dhan API call sites read the token via _get_dhan_token()."""

    @patch("dhan_data.requests.get")
    def test_init_dhan_uses_get_dhan_token(self, mock_get, tmp_path: Path) -> None:
        from dhan_data import init_dhan

        token_file = tmp_path / "dhan.token"
        token_file.write_text("init-token")

        mock_resp = type("R", (), {"status_code": 200})()
        mock_get.return_value = mock_resp

        with patch("dhan_data._DHAN_TOKEN_PATH", str(token_file)), \
             patch.dict(os.environ, {"DHAN_CLIENT_ID": "cid"}, clear=False):
            creds = init_dhan()

        sent_headers = mock_get.call_args.kwargs["headers"]
        assert sent_headers["access-token"] == "init-token"
        assert creds["client_id"] == "cid"
        # Backwards compat: returned dict still carries access_token
        assert creds["access_token"] == "init-token"

    def test_helper_picks_up_file_edits_between_calls(self, tmp_path: Path) -> None:
        """Sanity check that the helper itself rotates correctly.

        Static inspection (Task 2 step 4) verifies that lines 430/712/888 in
        dhan_data.py call _get_dhan_token() instead of dhan_creds["access_token"];
        mocking the full Dhan HTTP plumbing for those three sites would be
        brittle, so we lean on the static check + this helper test.
        """
        token_file = tmp_path / "dhan.token"
        token_file.write_text("v1")

        with patch("dhan_data._DHAN_TOKEN_PATH", str(token_file)):
            assert _get_dhan_token() == "v1"
            token_file.write_text("v2")
            assert _get_dhan_token() == "v2"
