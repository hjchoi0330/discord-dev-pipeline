"""Common module for invoking the Claude Code CLI."""
import logging
import os
import subprocess

logger = logging.getLogger(__name__)


def clean_env() -> dict[str, str]:
    """Return a clean environment dict with all Claude Code-related variables removed.

    Strips environment variables set when Claude Code is running (CLAUDECODE,
    CLAUDE_CODE_*, CLAUDE_AGENT_*, etc.) to prevent nested session detection.
    """
    return {
        k: v for k, v in os.environ.items()
        if not k.startswith("CLAUDE") and k != "CLAUDECODE"
    }


def call_claude(prompt: str, timeout: int = 120) -> str:
    """Invoke the Claude Code CLI via subprocess (no API key required).

    Args:
        prompt: Prompt text to pass to Claude.
        timeout: Subprocess timeout in seconds.

    Returns:
        Response text from Claude.

    Raises:
        RuntimeError: If the Claude CLI exits with a non-zero code or is not found.
        subprocess.TimeoutExpired: If the subprocess times out.
    """
    env = clean_env()
    try:
        result = subprocess.run(
            ["claude", "--print"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "claude CLI not found. Please verify that Claude Code is installed. "
            "(https://docs.anthropic.com/en/docs/claude-code)"
        )
    if result.returncode != 0:
        # stderr may be empty, so log stdout as well
        error_detail = result.stderr.strip() or result.stdout.strip()
        logger.error(
            "Claude CLI exited abnormally (code %d): %s",
            result.returncode, error_detail[:300],
        )
        raise RuntimeError(
            f"claude CLI error (code {result.returncode}): {error_detail[:500]}"
        )
    return result.stdout
