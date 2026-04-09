"""Tests for Claude CLI retry logic with exponential backoff."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

import shared.claude_cli as _claude_cli_mod
from shared.claude_cli import call_claude, _sleep_with_backoff


class TestSleepWithBackoff:
    @patch("shared.claude_cli.time.sleep")
    @patch("shared.claude_cli.random.uniform", return_value=0.5)
    def test_backoff_calculation(self, mock_uniform, mock_sleep):
        # attempt 0: delay = min(2 * 2^0, 30) = 2, jitter = 0.5, total = 2.5
        actual = _sleep_with_backoff(attempt=0, base_delay=2.0, max_delay=30.0)
        mock_sleep.assert_called_once_with(2.5)
        assert actual == 2.5

    @patch("shared.claude_cli.time.sleep")
    @patch("shared.claude_cli.random.uniform", return_value=1.0)
    def test_backoff_increases_with_attempt(self, mock_uniform, mock_sleep):
        # attempt 2: delay = min(2 * 2^2, 30) = 8, jitter = 1.0, total = 9.0
        actual = _sleep_with_backoff(attempt=2, base_delay=2.0, max_delay=30.0)
        mock_sleep.assert_called_once_with(9.0)
        assert actual == 9.0

    @patch("shared.claude_cli.time.sleep")
    @patch("shared.claude_cli.random.uniform", return_value=0.0)
    def test_backoff_capped_at_max_delay(self, mock_uniform, mock_sleep):
        # attempt 10: delay = min(2 * 2^10, 30) = 30 (capped), jitter = 0.0
        actual = _sleep_with_backoff(attempt=10, base_delay=2.0, max_delay=30.0)
        mock_sleep.assert_called_once_with(30.0)
        assert actual == 30.0


class TestCallClaudeRetry:
    def setup_method(self):
        _claude_cli_mod._version_logged = True  # skip version check in tests

    @patch("shared.claude_cli._sleep_with_backoff", return_value=0.0)
    @patch("shared.claude_cli.subprocess.run")
    def test_retries_on_nonzero_exit_then_succeeds(self, mock_run, mock_sleep):
        fail = MagicMock(returncode=1, stdout="", stderr="transient error")
        success = MagicMock(returncode=0, stdout="ok", stderr="")
        mock_run.side_effect = [fail, success]

        result = call_claude("prompt", max_retries=2)
        assert result == "ok"
        assert mock_run.call_count == 2
        mock_sleep.assert_called_once()

    @patch("shared.claude_cli._sleep_with_backoff", return_value=0.0)
    @patch("shared.claude_cli.subprocess.run")
    def test_retries_on_timeout_then_succeeds(self, mock_run, mock_sleep):
        mock_run.side_effect = [
            subprocess.TimeoutExpired(cmd="claude", timeout=10),
            MagicMock(returncode=0, stdout="ok", stderr=""),
        ]

        result = call_claude("prompt", timeout=10, max_retries=2)
        assert result == "ok"
        assert mock_run.call_count == 2

    @patch("shared.claude_cli._sleep_with_backoff", return_value=0.0)
    @patch("shared.claude_cli.subprocess.run")
    def test_exhausts_retries_on_persistent_failure(self, mock_run, mock_sleep):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="persistent error",
        )

        with pytest.raises(RuntimeError, match="claude CLI error"):
            call_claude("prompt", max_retries=2)

        # 1 initial + 2 retries = 3 calls
        assert mock_run.call_count == 3
        # 2 backoff sleeps (before retry 1 and retry 2)
        assert mock_sleep.call_count == 2

    @patch("shared.claude_cli._sleep_with_backoff", return_value=0.0)
    @patch("shared.claude_cli.subprocess.run")
    def test_exhausts_retries_on_persistent_timeout(self, mock_run, mock_sleep):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=10)

        with pytest.raises(subprocess.TimeoutExpired):
            call_claude("prompt", timeout=10, max_retries=1)

        # 1 initial + 1 retry = 2 calls
        assert mock_run.call_count == 2

    @patch("shared.claude_cli.subprocess.run")
    def test_no_retry_on_file_not_found(self, mock_run):
        mock_run.side_effect = FileNotFoundError("No such file")

        with pytest.raises(RuntimeError, match="claude CLI not found"):
            call_claude("prompt", max_retries=3)

        # Should not retry — FileNotFoundError is not retryable
        assert mock_run.call_count == 1

    @patch("shared.claude_cli.subprocess.run")
    def test_zero_retries_no_backoff(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="fail",
        )

        with pytest.raises(RuntimeError):
            call_claude("prompt", max_retries=0)

        assert mock_run.call_count == 1

    @patch("shared.claude_cli._sleep_with_backoff", return_value=0.0)
    @patch("shared.claude_cli.subprocess.run")
    def test_success_on_first_try_no_backoff(self, mock_run, mock_sleep):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        result = call_claude("prompt", max_retries=3)
        assert result == "ok"
        assert mock_run.call_count == 1
        mock_sleep.assert_not_called()

    @patch("shared.claude_cli._sleep_with_backoff", return_value=0.0)
    @patch("shared.claude_cli.subprocess.run")
    def test_backoff_params_passed_through(self, mock_run, mock_sleep):
        fail = MagicMock(returncode=1, stdout="", stderr="err")
        success = MagicMock(returncode=0, stdout="ok", stderr="")
        mock_run.side_effect = [fail, success]

        call_claude("prompt", max_retries=1, base_delay=5.0, max_delay=60.0)
        mock_sleep.assert_called_once_with(0, 5.0, 60.0)
