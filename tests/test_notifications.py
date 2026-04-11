"""Tests for notifications.py — Telegram push notifications."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from notifications import send_telegram


class TestSendTelegram:
    """Tests for send_telegram helper."""

    @patch("requests.post")
    @patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok123", "TELEGRAM_CHAT_ID": "chat456"})
    def test_success(self, mock_post: MagicMock):
        """Mock env vars set, requests.post returns 200 -> True."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        result = send_telegram("hello")

        assert result is True
        mock_post.assert_called_once()

    @patch("requests.post")
    @patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok123", "TELEGRAM_CHAT_ID": "chat456"})
    def test_failure_status_code(self, mock_post: MagicMock):
        """Mock 400 response -> False."""
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "Bad Request"
        mock_post.return_value = mock_resp

        result = send_telegram("hello")

        assert result is False

    @patch("requests.post")
    @patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": "chat456"}, clear=True)
    def test_missing_token(self, mock_post: MagicMock):
        """No TELEGRAM_BOT_TOKEN -> False (no request made)."""
        result = send_telegram("hello")

        assert result is False
        mock_post.assert_not_called()

    @patch("requests.post")
    @patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok123", "TELEGRAM_CHAT_ID": ""}, clear=True)
    def test_missing_chat_id(self, mock_post: MagicMock):
        """No TELEGRAM_CHAT_ID -> False."""
        result = send_telegram("hello")

        assert result is False
        mock_post.assert_not_called()

    @patch("requests.post", side_effect=ConnectionError("network down"))
    @patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok123", "TELEGRAM_CHAT_ID": "chat456"})
    def test_network_exception(self, mock_post: MagicMock):
        """requests.post raises ConnectionError -> False."""
        result = send_telegram("hello")

        assert result is False

    @patch("requests.post")
    @patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "mytoken", "TELEGRAM_CHAT_ID": "mychat"})
    def test_correct_url_and_payload(self, mock_post: MagicMock):
        """Verify the URL uses the token and payload has chat_id, text, parse_mode."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        send_telegram("test message")

        mock_post.assert_called_once_with(
            "https://api.telegram.org/botmytoken/sendMessage",
            json={"chat_id": "mychat", "text": "test message", "parse_mode": "HTML"},
            timeout=10,
        )
