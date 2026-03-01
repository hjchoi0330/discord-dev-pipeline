"""Claude Code CLI 호출 공통 모듈."""
import logging
import os
import subprocess

logger = logging.getLogger(__name__)


def clean_env() -> dict[str, str]:
    """Claude Code 관련 환경변수를 모두 제거한 깨끗한 env를 반환합니다.

    Claude Code가 실행 중일 때 설정되는 환경변수(CLAUDECODE, CLAUDE_CODE_*,
    CLAUDE_AGENT_* 등)를 제거하여 nested session 감지를 방지합니다.
    """
    return {
        k: v for k, v in os.environ.items()
        if not k.startswith("CLAUDE") and k != "CLAUDECODE"
    }


def call_claude(prompt: str, timeout: int = 120) -> str:
    """Claude Code CLI를 subprocess로 호출합니다 (API 키 불필요).

    Args:
        prompt: Claude에 전달할 프롬프트 텍스트.
        timeout: subprocess 타임아웃 (초).

    Returns:
        Claude의 응답 텍스트.

    Raises:
        RuntimeError: Claude CLI가 비정상 종료하거나 찾을 수 없는 경우.
        subprocess.TimeoutExpired: 타임아웃 초과.
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
            "claude CLI를 찾을 수 없습니다. Claude Code가 설치되어 있는지 확인하세요. "
            "(https://docs.anthropic.com/en/docs/claude-code)"
        )
    if result.returncode != 0:
        # stderr가 비어있을 수 있으므로 stdout도 함께 로깅
        error_detail = result.stderr.strip() or result.stdout.strip()
        logger.error(
            "Claude CLI 비정상 종료 (코드 %d): %s",
            result.returncode, error_detail[:300],
        )
        raise RuntimeError(
            f"claude CLI 오류 (코드 {result.returncode}): {error_detail[:500]}"
        )
    return result.stdout
