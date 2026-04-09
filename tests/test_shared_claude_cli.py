"""Unit tests for the shared.claude_cli module."""

from __future__ import annotations

import subprocess
from unittest.mock import patch, MagicMock, call

import pytest

import shared.claude_cli as _claude_cli_mod
from shared.claude_cli import call_claude


class TestCallClaude:
    def setup_method(self):
        # Reset the module-level flag before each test so version logging
        # behaviour is deterministic regardless of test execution order.
        _claude_cli_mod._version_logged = False

    @patch("shared.claude_cli.subprocess.run")
    def test_successful_call(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Hello from Claude",
            stderr="",
        )
        result = call_claude("test prompt")
        assert result == "Hello from Claude"
        # First call is the main claude --print invocation.
        first_call = mock_run.call_args_list[0]
        assert first_call[0][0] == ["claude", "--print"]
        assert first_call[1]["input"] == "test prompt"
        # Second call is the one-time version check.
        assert mock_run.call_count == 2
        second_call = mock_run.call_args_list[1]
        assert second_call[0][0] == ["claude", "--version"]

    @patch("shared.claude_cli.subprocess.run")
    def test_nonzero_exit_raises_runtime_error(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Error: something went wrong",
        )
        with pytest.raises(RuntimeError, match="claude CLI error"):
            call_claude("test prompt")

    @patch("shared.claude_cli.subprocess.run")
    def test_timeout_propagates(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=120)
        with pytest.raises(subprocess.TimeoutExpired):
            call_claude("test prompt", timeout=120)

    @patch("shared.claude_cli.subprocess.run")
    def test_claude_not_found_raises_runtime_error(self, mock_run):
        mock_run.side_effect = FileNotFoundError("No such file")
        with pytest.raises(RuntimeError, match="claude CLI not found"):
            call_claude("test prompt")

    @patch("shared.claude_cli.subprocess.run")
    def test_custom_timeout(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        call_claude("prompt", timeout=300)
        # First call is claude --print; verify its timeout matches the argument.
        assert mock_run.call_args_list[0][1]["timeout"] == 300
