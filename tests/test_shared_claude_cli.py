"""shared.claude_cli 모듈 단위 테스트."""

from __future__ import annotations

import subprocess
from unittest.mock import patch, MagicMock

import pytest

from shared.claude_cli import call_claude


class TestCallClaude:
    @patch("shared.claude_cli.subprocess.run")
    def test_successful_call(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Hello from Claude",
            stderr="",
        )
        result = call_claude("test prompt")
        assert result == "Hello from Claude"
        mock_run.assert_called_once()
        args = mock_run.call_args
        assert args[0][0] == ["claude", "--print"]
        assert args[1]["input"] == "test prompt"

    @patch("shared.claude_cli.subprocess.run")
    def test_nonzero_exit_raises_runtime_error(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="Error: something went wrong",
        )
        with pytest.raises(RuntimeError, match="claude CLI 오류"):
            call_claude("test prompt")

    @patch("shared.claude_cli.subprocess.run")
    def test_timeout_propagates(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=120)
        with pytest.raises(subprocess.TimeoutExpired):
            call_claude("test prompt", timeout=120)

    @patch("shared.claude_cli.subprocess.run")
    def test_claude_not_found_raises_runtime_error(self, mock_run):
        mock_run.side_effect = FileNotFoundError("No such file")
        with pytest.raises(RuntimeError, match="claude CLI를 찾을 수 없습니다"):
            call_claude("test prompt")

    @patch("shared.claude_cli.subprocess.run")
    def test_custom_timeout(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        call_claude("prompt", timeout=300)
        assert mock_run.call_args[1]["timeout"] == 300
