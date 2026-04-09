"""Executor module: reads plan markdown files and runs Claude Code CLI."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.claude_cli import clean_env as _clean_claude_env
from shared.plan_format import PLAN_PROMPT_SECTION

logger = logging.getLogger(__name__)


@dataclass
class ExecutionResult:
    """Result of executing a single plan file."""

    plan_file: str
    prompt_used: str
    claude_output: str
    success: bool
    error: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _find_claude_binary(cli_path: str = "claude") -> str | None:
    """Return the resolved path to the claude binary, or None if not found."""
    resolved = shutil.which(cli_path)
    if resolved:
        return resolved
    # Also try common install locations
    for candidate in ["/usr/local/bin/claude", "/opt/homebrew/bin/claude"]:
        if Path(candidate).exists():
            return candidate
    return None


def _extract_prompt(plan_text: str) -> str:
    """Extract the content under the prompt section defined in shared.plan_format."""
    pattern = re.compile(
        rf"##\s+{re.escape(PLAN_PROMPT_SECTION)}\s*\n(.*?)(?=\n##\s|\Z)",
        re.DOTALL,
    )
    match = pattern.search(plan_text)
    if match:
        return match.group(1).strip()
    return ""


def _extract_project_dir(
    plan_text: str,
    data_dir: Path,
    plan_file: Path | None = None,
) -> Path:
    """Determine the project directory from the plan content.

    Priority:
      1. Extract 'data/result/<dirname>' pattern from the plan body
      2. Reuse an existing directory under data/result/ whose keywords overlap
      3. Create a new directory based on the plan filename

    Args:
        plan_text: Full markdown text of the plan.
        data_dir: Root data directory.
        plan_file: Path to the plan file (used to derive the directory name).
    """
    result_dir = data_dir / "result"

    # 1) Use the explicit path if present in the plan body
    match = re.search(r"data/result/([\w-]+)", plan_text)
    if match:
        candidate = result_dir / match.group(1)
        if not candidate.resolve().is_relative_to(result_dir.resolve()):
            logger.warning(
                "Extracted project_dir %s escapes result_dir; falling back to default-project.",
                candidate,
            )
            return result_dir / "default-project"
        return candidate

    # 2) Extract topic keywords from the plan filename
    topic_name = ""
    if plan_file:
        # '2026-03-01_docker-oom-memory-limit.md' -> 'docker-oom-memory-limit'
        stem = plan_file.stem
        # Strip the date prefix (YYYY-MM-DD_)
        topic_name = re.sub(r"^\d{4}-\d{2}-\d{2}_", "", stem)

    # 3) Search for a related existing project directory by keyword matching
    if topic_name and result_dir.exists():
        topic_keywords = set(topic_name.lower().replace("_", "-").split("-"))
        # Remove meaningless short words
        topic_keywords = {k for k in topic_keywords if len(k) >= 2}

        best_match: Path | None = None
        best_score = 0
        for existing in result_dir.iterdir():
            if not existing.is_dir():
                continue
            dir_keywords = set(existing.name.lower().replace("_", "-").split("-"))
            overlap = topic_keywords & dir_keywords
            score = len(overlap)
            if score > best_score and score >= 2:
                best_score = score
                best_match = existing

        if best_match:
            logger.info(
                "Reusing existing project directory: %s (match score: %d)",
                best_match.name, best_score,
            )
            return best_match

    # 4) Create a new directory from the topic name, or fall back to default-project
    if topic_name:
        safe_name = re.sub(r"[^a-zA-Z0-9_-]", "-", topic_name).strip("-")
        if safe_name:
            candidate = result_dir / safe_name
            if not candidate.resolve().is_relative_to(result_dir.resolve()):
                logger.warning(
                    "Derived project_dir %s escapes result_dir; falling back to default-project.",
                    candidate,
                )
                return result_dir / "default-project"
            return candidate

    return result_dir / "default-project"


def _save_pending(plan_file: Path, prompt: str, data_dir: Path) -> Path:
    """Save a prompt to data/pending_executions/ for manual review."""
    pending_dir = data_dir / "pending_executions"
    pending_dir.mkdir(parents=True, exist_ok=True)

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Derive a safe topic name from the plan filename
    topic = re.sub(r"[^a-zA-Z0-9_-]", "_", plan_file.stem)
    out_path = pending_dir / f"{date_str}_{topic}_prompt.txt"

    out_path.write_text(prompt, encoding="utf-8")
    logger.warning(
        "claude binary not found. Prompt saved for manual review: %s", out_path
    )
    return out_path


def _load_executed_manifest(data_dir: Path) -> dict[str, Any]:
    """Load the executed-plans manifest from data/plans/.executed.json.

    Returns a dict mapping plan filenames to execution metadata.
    """
    manifest_path = data_dir / "meta" / "executed.json"
    if not manifest_path.exists():
        return {}
    try:
        with manifest_path.open(encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _mark_plan_executed(plan_file: Path, data_dir: Path) -> None:
    """Record a plan as successfully executed in the manifest."""
    manifest_path = data_dir / "meta" / "executed.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    manifest = _load_executed_manifest(data_dir)
    manifest[plan_file.name] = {
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "plan_path": str(plan_file),
    }
    with manifest_path.open("w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)


def _append_execution_log(result: ExecutionResult, data_dir: Path) -> None:
    """Append an ExecutionResult to today's execution log JSON."""
    executions_dir = data_dir / "executions"
    executions_dir.mkdir(parents=True, exist_ok=True)

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_path = executions_dir / f"{date_str}_execution_log.json"

    records: list[dict[str, Any]] = []
    if log_path.exists():
        try:
            with log_path.open(encoding="utf-8") as fh:
                records = json.load(fh)
        except (json.JSONDecodeError, OSError):
            logger.warning("Could not read existing log %s; starting fresh.", log_path)

    records.append(result.to_dict())
    with log_path.open("w", encoding="utf-8") as fh:
        json.dump(records, fh, ensure_ascii=False, indent=2)


def execute_plan(
    plan_file: Path,
    dry_run: bool = False,
    data_dir: Path | None = None,
    claude_cli_path: str = "claude",
    timeout_seconds: int = 300,
    project_dir: Path | None = None,
) -> ExecutionResult:
    """Execute a single plan file using the Claude Code CLI.

    Args:
        plan_file: Path to the markdown plan file.
        dry_run: If True, log what would be executed without running anything.
        data_dir: Root data directory (defaults to data/ relative to cwd).
        claude_cli_path: Name or path of the claude binary.
        timeout_seconds: Timeout for the subprocess call.
        project_dir: Working directory for the claude subprocess. If None,
            extracted from the plan text or defaults to data/result/default-project.

    Returns:
        An ExecutionResult describing the outcome.
    """
    if data_dir is None:
        data_dir = Path("data")

    if not plan_file.exists():
        err = f"Plan file not found: {plan_file}"
        logger.error(err)
        result = ExecutionResult(
            plan_file=str(plan_file),
            prompt_used="",
            claude_output="",
            success=False,
            error=err,
        )
        _append_execution_log(result, data_dir)
        return result

    plan_text = plan_file.read_text(encoding="utf-8")
    prompt = _extract_prompt(plan_text)

    if not prompt:
        err = (
            f"No '## {PLAN_PROMPT_SECTION}' section found in {plan_file}. "
            "Skipping execution."
        )
        logger.warning(err)
        result = ExecutionResult(
            plan_file=str(plan_file),
            prompt_used="",
            claude_output="",
            success=False,
            error=err,
        )
        _append_execution_log(result, data_dir)
        return result

    if dry_run:
        logger.info("[DRY RUN] Would execute plan: %s", plan_file.name)
        logger.info("[DRY RUN] Prompt (first 300 chars):\n%s", prompt[:300])
        result = ExecutionResult(
            plan_file=str(plan_file),
            prompt_used=prompt,
            claude_output="[dry run - not executed]",
            success=True,
            error="",
        )
        _append_execution_log(result, data_dir)
        return result

    claude_bin = _find_claude_binary(claude_cli_path)
    if claude_bin is None:
        pending_path = _save_pending(plan_file, prompt, data_dir)
        result = ExecutionResult(
            plan_file=str(plan_file),
            prompt_used=prompt,
            claude_output="",
            success=False,
            error=(
                f"claude binary not found in PATH. "
                f"Prompt saved to {pending_path} for manual review."
            ),
        )
        _append_execution_log(result, data_dir)
        return result

    # Determine and create the working directory for the subprocess
    if project_dir is None:
        project_dir = _extract_project_dir(plan_text, data_dir, plan_file)
    project_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Executing plan: %s", plan_file.name)
    logger.info("Using claude binary: %s", claude_bin)
    logger.info("Working directory: %s", project_dir)

    # Remove all Claude Code env vars to avoid nested session detection
    env = _clean_claude_env()
    try:
        proc = subprocess.run(
            [claude_bin, "--print", "--dangerously-skip-permissions"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            encoding="utf-8",
            cwd=str(project_dir),
            env=env,
        )
        success = proc.returncode == 0
        claude_output = proc.stdout
        error = proc.stderr if not success else ""

        if success:
            logger.info("Plan executed successfully: %s", plan_file.name)
            _mark_plan_executed(plan_file, data_dir)
        else:
            logger.error(
                "Claude exited with code %d for plan %s: %s",
                proc.returncode,
                plan_file.name,
                error[:500],
            )

        result = ExecutionResult(
            plan_file=str(plan_file),
            prompt_used=prompt,
            claude_output=claude_output,
            success=success,
            error=error,
        )
    except subprocess.TimeoutExpired:
        err = f"Claude timed out after {timeout_seconds}s for plan {plan_file.name}"
        logger.error(err)
        result = ExecutionResult(
            plan_file=str(plan_file),
            prompt_used=prompt,
            claude_output="",
            success=False,
            error=err,
        )
    except OSError as exc:
        err = f"Failed to run claude binary: {exc}"
        logger.error(err)
        result = ExecutionResult(
            plan_file=str(plan_file),
            prompt_used=prompt,
            claude_output="",
            success=False,
            error=err,
        )

    _append_execution_log(result, data_dir)
    return result


def execute_plans(
    plan_files: list[Path],
    dry_run: bool = False,
    data_dir: Path | None = None,
    claude_cli_path: str = "claude",
    timeout_seconds: int = 300,
    force: bool = False,
) -> list[ExecutionResult]:
    """Execute multiple plan files sequentially.

    Plans that have already been executed successfully are skipped unless
    *force* is True.

    Args:
        plan_files: List of plan markdown file paths.
        dry_run: If True, only log what would be executed.
        data_dir: Root data directory.
        claude_cli_path: Name or path of the claude binary.
        timeout_seconds: Per-plan timeout for the subprocess call.
        force: If True, re-execute plans even if already completed.

    Returns:
        List of ExecutionResult, one per plan file.
    """
    if data_dir is None:
        data_dir = Path("data")

    if not plan_files:
        logger.info("No plan files to execute.")
        return []

    # Filter out already-executed plans unless force is set
    manifest = _load_executed_manifest(data_dir)
    if not force and manifest:
        pending = [p for p in plan_files if p.name not in manifest]
        skipped = len(plan_files) - len(pending)
        if skipped:
            logger.info(
                "Skipping %d already-executed plan(s). Use --force to re-execute.",
                skipped,
            )
        plan_files = pending

    if not plan_files:
        logger.info("All plans have already been executed. Nothing to do.")
        return []

    results: list[ExecutionResult] = []
    for i, plan_file in enumerate(plan_files, start=1):
        logger.info("Executing plan %d/%d: %s", i, len(plan_files), plan_file.name)
        result = execute_plan(
            plan_file=plan_file,
            dry_run=dry_run,
            data_dir=data_dir,
            claude_cli_path=claude_cli_path,
            timeout_seconds=timeout_seconds,
        )
        results.append(result)

    succeeded = sum(1 for r in results if r.success)
    logger.info(
        "Execution complete: %d/%d plans succeeded.", succeeded, len(results)
    )
    return results
