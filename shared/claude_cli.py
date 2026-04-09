"""Common module for invoking the Claude Code CLI."""
import logging
import os
import random
import subprocess
import time

logger = logging.getLogger(__name__)

_version_logged: bool = False

# Retry defaults
DEFAULT_MAX_RETRIES = 3
DEFAULT_BASE_DELAY = 2.0
DEFAULT_MAX_DELAY = 30.0


def clean_env() -> dict[str, str]:
    """Return a clean environment dict with all Claude Code-related variables removed.

    Strips environment variables set when Claude Code is running (CLAUDECODE,
    CLAUDE_CODE_*, CLAUDE_AGENT_*, etc.) to prevent nested session detection.
    """
    return {
        k: v for k, v in os.environ.items()
        if not k.startswith("CLAUDE") and k != "CLAUDECODE"
    }


def _sleep_with_backoff(attempt: int, base_delay: float, max_delay: float) -> float:
    """Sleep with exponential backoff + jitter. Returns the actual delay used."""
    delay = min(base_delay * (2 ** attempt), max_delay)
    jitter = random.uniform(0, delay * 0.5)
    actual = delay + jitter
    time.sleep(actual)
    return actual


def call_claude(
    prompt: str,
    timeout: int = 120,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
) -> str:
    """Invoke the Claude Code CLI via subprocess with retry logic.

    Args:
        prompt: Prompt text to pass to Claude.
        timeout: Subprocess timeout in seconds.
        max_retries: Maximum number of retry attempts (0 = no retries).
        base_delay: Base delay in seconds for exponential backoff.
        max_delay: Maximum delay cap in seconds.

    Returns:
        Response text from Claude.

    Raises:
        RuntimeError: If the Claude CLI exits with a non-zero code or is not found
            after all retries are exhausted.
        subprocess.TimeoutExpired: If the subprocess times out after all retries.
    """
    env = clean_env()
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
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
            # Not retryable — claude binary is missing
            raise RuntimeError(
                "claude CLI not found. Please verify that Claude Code is installed. "
                "(https://docs.anthropic.com/en/docs/claude-code)"
            )
        except subprocess.TimeoutExpired:
            last_error = subprocess.TimeoutExpired(
                cmd=["claude", "--print"], timeout=timeout,
            )
            if attempt < max_retries:
                delay = _sleep_with_backoff(attempt, base_delay, max_delay)
                logger.warning(
                    "Claude CLI timed out (attempt %d/%d). Retrying in %.1fs...",
                    attempt + 1, max_retries + 1, delay,
                )
                continue
            raise last_error

        if result.returncode != 0:
            error_detail = result.stderr.strip() or result.stdout.strip()
            last_error = RuntimeError(
                f"claude CLI error (code {result.returncode}): {error_detail[:500]}"
            )
            if attempt < max_retries:
                delay = _sleep_with_backoff(attempt, base_delay, max_delay)
                logger.warning(
                    "Claude CLI exited with code %d (attempt %d/%d). Retrying in %.1fs...",
                    result.returncode, attempt + 1, max_retries + 1, delay,
                )
                continue
            logger.error(
                "Claude CLI exited abnormally (code %d): %s",
                result.returncode, error_detail[:300],
            )
            raise last_error

        # Success
        global _version_logged
        if not _version_logged:
            _version_logged = True
            try:
                ver = subprocess.run(
                    ["claude", "--version"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    env=env,
                )
                logger.info("Claude CLI version: %s", ver.stdout.strip() or ver.stderr.strip())
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not determine Claude CLI version: %s", exc)

        if attempt > 0:
            logger.info("Claude CLI succeeded on attempt %d/%d.", attempt + 1, max_retries + 1)

        return result.stdout

    # Should not reach here, but just in case
    raise last_error or RuntimeError("call_claude failed unexpectedly")
