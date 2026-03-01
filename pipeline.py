#!/usr/bin/env python3
"""Discord Dev Pipeline - Main orchestrator.

Data flow:
  Collect Discord conversations → Analyze dev topics → Generate plans → Execute with Claude Code
"""

from __future__ import annotations

import argparse
import json as _json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

from shared.pipeline_state import (
    PipelineRun,
    save_run,
    load_runs,
    STAGE_COLLECT,
    STAGE_ANALYZE,
    STAGE_PLAN,
    STAGE_EXECUTE,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

_CONFIG_PATH = Path("config.yaml")


def _resolve_data_dir() -> Path:
    """Resolve the data directory from DATA_DIR env var or config.yaml.

    Priority: DATA_DIR env var > config.yaml pipeline.data_dir > "data"
    """
    env_val = os.environ.get("DATA_DIR")
    if env_val:
        return Path(env_val)
    if _CONFIG_PATH.exists():
        with _CONFIG_PATH.open(encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        cfg_val = cfg.get("pipeline", {}).get("data_dir")
        if cfg_val:
            return Path(cfg_val)
    return Path("data")


_DATA_DIR = _resolve_data_dir()


def _load_config() -> dict:
    if _CONFIG_PATH.exists():
        with _CONFIG_PATH.open(encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def _find_conversation_files(date: str) -> list[Path]:
    """Search for conversation files matching the given date.

    Also includes files from the previous day to avoid missing conversations
    that span a date boundary (e.g., a recording started at 23:00 and ended at 01:00 the next day).
    """
    conv_dir = _DATA_DIR / "conversations"
    if not conv_dir.exists():
        return []
    target = datetime.strptime(date, "%Y-%m-%d")
    prev_date = (target - timedelta(days=1)).strftime("%Y-%m-%d")
    files = set(conv_dir.glob(f"{date}_*.json"))
    files |= set(conv_dir.glob(f"{prev_date}_*.json"))
    return sorted(files)


def _load_existing_analysis(date: str):
    """Load a previously saved AnalysisResult from disk.

    Returns the reconstructed AnalysisResult or None if unavailable.
    """
    from analyzer.analyzer import AnalysisResult, DevTopic, Message

    analysis_path = _DATA_DIR / "analysis" / f"{date}_analysis.json"
    if not analysis_path.exists():
        return None
    try:
        with analysis_path.open(encoding="utf-8") as f:
            data = _json.load(f)
        topics = []
        for t in data.get("dev_topics", []):
            msgs = [
                Message(
                    author=m.get("author", ""),
                    content=m.get("content", ""),
                    timestamp=m.get("timestamp", ""),
                )
                for m in t.get("messages", [])
            ]
            topics.append(DevTopic(
                id=t.get("id", ""),
                title=t.get("title", ""),
                category=t.get("category", ""),
                priority=t.get("priority", "medium"),
                messages=msgs,
                summary=t.get("summary", ""),
                keywords=t.get("keywords", []),
                actionable=t.get("actionable", False),
                estimated_complexity=t.get("estimated_complexity", "medium"),
            ))
        return AnalysisResult(
            date=data.get("date", date),
            analyzed_at=data.get("analyzed_at", ""),
            source_files=data.get("source_files", []),
            dev_topics=topics,
            total_messages_analyzed=data.get("total_messages_analyzed", 0),
            dev_topics_found=data.get("dev_topics_found", 0),
        )
    except (_json.JSONDecodeError, OSError, KeyError):
        return None


def _get_already_analyzed_files(date: str) -> tuple[set[str], float]:
    """Return previously analyzed file paths and the analysis timestamp.

    Uses the existing analysis.json to avoid redundant processing.
    Returns (set_of_file_paths, analysis_mtime) where analysis_mtime is
    the modification time of the analysis file (0.0 if not found).
    """
    analysis_path = _DATA_DIR / "analysis" / f"{date}_analysis.json"
    if not analysis_path.exists():
        return set(), 0.0
    try:
        analysis_mtime = analysis_path.stat().st_mtime
        with analysis_path.open(encoding="utf-8") as f:
            data = _json.load(f)
        return set(data.get("source_files", [])), analysis_mtime
    except (_json.JSONDecodeError, OSError):
        return set(), 0.0


def run_pipeline(
    date: str | None = None,
    dry_run: bool = False,
    force: bool = False,
    auto_execute: bool | None = None,
) -> None:
    """Run the full pipeline.

    Tracks progress of each stage with PipelineRun and saves the run history
    to data/pipeline_runs/.

    Args:
        date: Date to analyze (YYYY-MM-DD). Defaults to today if None.
        dry_run: If True, skips actual Claude Code execution.
        force: If True, re-analyzes even dates that have already been analyzed.
        auto_execute: If True, forces plan execution to be enabled.
            If None, uses the pipeline.auto_execute value from config.yaml.
    """
    from analyzer.analyzer import analyze_conversations
    from planner.planner import generate_plans
    from executor.executor import execute_plans

    config = _load_config()
    pipeline_cfg = config.get("pipeline", {})
    executor_cfg = config.get("executor", {})

    if date is None:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    run = PipelineRun.create(date, config={"dry_run": dry_run, "force": force, **pipeline_cfg})
    logger.info("=== Discord Dev Pipeline started (date: %s, run: %s) ===", date, run.run_id)

    try:
        # ── Stage 1: Collect ─────────────────────────────────────────
        collect_stage = run.get_stage(STAGE_COLLECT)
        collect_stage.start()

        conv_files = _find_conversation_files(date)
        if not conv_files:
            collect_stage.fail("No conversation files found for the given date")
            logger.warning("No conversation files found for date %s.", date)
            run.get_stage(STAGE_ANALYZE).skip("No input files")
            run.get_stage(STAGE_PLAN).skip("No input files")
            run.get_stage(STAGE_EXECUTE).skip("No input files")
            return

        collect_stage.complete({
            "files_found": len(conv_files),
            "file_names": [f.name for f in conv_files],
        })

        # ── Stage 2: Analyze ─────────────────────────────────────────
        analyze_stage = run.get_stage(STAGE_ANALYZE)
        analyze_stage.start({"input_files": len(conv_files)})

        analysis = None

        if not force:
            already_analyzed, analysis_mtime = _get_already_analyzed_files(date)
            if already_analyzed:
                new_files = []
                for f in conv_files:
                    if str(f) not in already_analyzed:
                        new_files.append(f)
                    elif f.stat().st_mtime > analysis_mtime:
                        logger.info("File modified since last analysis: %s", f.name)
                        new_files.append(f)

                if not new_files:
                    logger.info("All conversation files have already been analyzed. Using cached analysis.")
                    analysis = _load_existing_analysis(date)
                    if analysis:
                        analyze_stage.complete({
                            "source": "cached",
                            "topics_found": analysis.dev_topics_found,
                        })
                    else:
                        analyze_stage.fail("Failed to load cached analysis file")
                else:
                    skipped = len(conv_files) - len(new_files)
                    if skipped > 0:
                        logger.info("Skipping %d already-analyzed file(s), %d new file(s) to analyze", skipped, len(new_files))
                    conv_files = new_files

        if analysis is None and analyze_stage.status != "failed":
            logger.info("Conversation files to analyze: %d", len(conv_files))
            for f in conv_files:
                logger.info("  - %s", f)
            logger.info("Analyzing conversation files...")
            analysis = analyze_conversations(conv_files, date, data_dir=_DATA_DIR)
            analyze_stage.complete({
                "source": "fresh",
                "topics_found": analysis.dev_topics_found,
                "messages_analyzed": analysis.total_messages_analyzed,
            })

        if analysis is None or not analysis.dev_topics:
            if analyze_stage.status != "failed":
                logger.info("No development-related topics found.")
            run.get_stage(STAGE_PLAN).skip("No dev topics found")
            run.get_stage(STAGE_EXECUTE).skip("No dev topics found")
            return

        logger.info("Development topics found: %d", analysis.dev_topics_found)
        for topic in analysis.dev_topics:
            actionable_mark = "O" if topic.actionable else "x"
            logger.info("  [%s] [%s] %s", actionable_mark, topic.priority.upper(), topic.title)

        # ── Stage 3: Plan ────────────────────────────────────────────
        plan_stage = run.get_stage(STAGE_PLAN)
        plan_stage.start({"actionable_topics": sum(1 for t in analysis.dev_topics if t.actionable)})

        plan_dir = _DATA_DIR / "plans"
        plan_files = generate_plans(analysis, plan_dir, data_dir=_DATA_DIR)

        plan_stage.complete({
            "plans_generated": len(plan_files),
            "plan_files": [p.name for p in plan_files],
        })

        if plan_files:
            logger.info("Plan files generated: %d", len(plan_files))
            for p in plan_files:
                logger.info("  - %s", p)

        # ── Stage 4: Execute ─────────────────────────────────────────
        execute_stage = run.get_stage(STAGE_EXECUTE)

        if not plan_files:
            execute_stage.skip("No new plans to execute")
            logger.info("No new plans to execute.")
        elif dry_run:
            execute_stage.skip("Dry run mode")
            logger.info("[DRY RUN] Skipping plan execution.")
        elif not (auto_execute if auto_execute is not None else pipeline_cfg.get("auto_execute", False)):
            execute_stage.skip("auto_execute disabled")
            logger.info("Auto-execute is disabled. Set pipeline.auto_execute to true in config.yaml.")
        else:
            execute_stage.start({"plans_to_execute": len(plan_files)})
            logger.info("Executing plans with Claude Code...")
            results = execute_plans(
                plan_files,
                dry_run=dry_run,
                data_dir=_DATA_DIR,
                claude_cli_path=executor_cfg.get("claude_cli_path", "claude"),
                timeout_seconds=executor_cfg.get("timeout_seconds", 300),
                force=force,
            )
            succeeded = sum(1 for r in results if r.success)
            execute_stage.complete({
                "succeeded": succeeded,
                "failed": len(results) - succeeded,
                "total": len(results),
            })
    finally:
        run.finish()
        save_run(run, _DATA_DIR)
        _print_run_summary(run)


def _print_run_summary(run: PipelineRun) -> None:
    """Print the pipeline run result broken down by stage."""
    logger.info("=== Pipeline Run Summary (%s) ===", run.run_id)
    logger.info("Date: %s | Status: %s", run.date, run.status)
    duration = run.duration_seconds()
    if duration is not None:
        logger.info("Total duration: %.1fs", duration)
    for stage in run.stages:
        marks = {"completed": "O", "failed": "X", "skipped": "-", "pending": "?", "running": "~"}
        mark = marks.get(stage.status, "?")
        parts = [f"  [{mark}] {stage.name}: {stage.status}"]
        stage_dur = stage.duration_seconds()
        if stage_dur is not None:
            parts.append(f"({stage_dur:.1f}s)")
        if stage.output_summary:
            details = ", ".join(f"{k}={v}" for k, v in stage.output_summary.items()
                                if k not in ("file_names", "plan_files"))
            if details:
                parts.append(f"- {details}")
        if stage.error and stage.status == "failed":
            parts.append(f"- error: {stage.error[:100]}")
        logger.info(" ".join(parts))
    logger.info("=== %s ===", run.summary_line())


def show_history(date: str | None = None) -> None:
    """Print the pipeline run history."""
    runs = load_runs(_DATA_DIR, date=date)
    if not runs:
        label = f"date {date}" if date else "all"
        print(f"\nNo pipeline run history found ({label}).")
        return

    print(f"\n=== Pipeline Run History ({len(runs)} run(s)) ===")
    for run in runs:
        print(f"\n{run.summary_line()}")
        duration = run.duration_seconds()
        if duration is not None:
            print(f"  Duration: {duration:.1f}s")
        for stage in run.stages:
            marks = {"completed": "O", "failed": "X", "skipped": "-", "pending": "?"}
            mark = marks.get(stage.status, "?")
            line = f"    [{mark}] {stage.name}"
            if stage.output_summary:
                details = {k: v for k, v in stage.output_summary.items()
                           if k not in ("file_names", "plan_files")}
                if details:
                    line += f"  {details}"
            if stage.error and stage.status == "failed":
                line += f"  (error: {stage.error[:80]})"
            print(line)


def run_interactive() -> None:
    """Interactive CLI menu."""
    from executor.executor import execute_plan

    print("\n=== Discord Dev Pipeline ===")
    while True:
        print("\nMenu:")
        print("  1. Run full pipeline for today")
        print("  2. Run pipeline for a specific date")
        print("  3. Run analysis only (no plan generation)")
        print("  4. List existing plan files")
        print("  5. Execute a specific plan file")
        print("  6. View pipeline run history")
        print("  7. Start Discord bot")
        print("  0. Exit")

        choice = input("\nChoice: ").strip()

        if choice == "1":
            run_pipeline()
        elif choice == "2":
            date = input("Enter date (YYYY-MM-DD): ").strip()
            run_pipeline(date=date)
        elif choice == "3":
            from analyzer.analyzer import analyze_conversations
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            files = _find_conversation_files(date)
            if not files:
                print(f"No conversation files found for date {date}.")
            else:
                result = analyze_conversations(files, date)
                print(f"\nAnalysis complete: {result.dev_topics_found} topic(s) found")
        elif choice == "4":
            plan_dir = _DATA_DIR / "plans"
            if plan_dir.exists():
                plans = sorted(plan_dir.glob("*.md"))
                if plans:
                    print(f"\n{len(plans)} plan file(s):")
                    for i, p in enumerate(plans, 1):
                        print(f"  {i}. {p.name}")
                else:
                    print("No plan files found.")
            else:
                print("Plans directory does not exist.")
        elif choice == "5":
            plan_dir = _DATA_DIR / "plans"
            plans = sorted(plan_dir.glob("*.md")) if plan_dir.exists() else []
            if not plans:
                print("No plan files found.")
            else:
                print(f"\n{len(plans)} plan file(s):")
                for i, p in enumerate(plans, 1):
                    print(f"  {i}. {p.name}")
                idx = input("\nEnter number to execute (0=cancel): ").strip()
                if idx.isdigit() and 1 <= int(idx) <= len(plans):
                    plan = plans[int(idx) - 1]
                    print(f"\nExecuting plan: {plan.name}")
                    result = execute_plan(plan, data_dir=_DATA_DIR)
                    status = "Success" if result.success else f"Failed: {result.error[:200]}"
                    print(f"Result: {status}")
                elif idx != "0":
                    print("Invalid number.")
        elif choice == "6":
            date_input = input("Date filter (YYYY-MM-DD, leave blank for all): ").strip() or None
            show_history(date=date_input)
        elif choice == "7":
            from collector.bot import run
            print("Starting Discord bot... (Press Ctrl+C to stop)")
            run()
        elif choice == "0":
            print("Exiting.")
            break
        else:
            print("Invalid choice.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Discord Dev Pipeline - Automatically generates and executes development plans from Discord conversations."
    )
    parser.add_argument("--run", action="store_true", help="Run the pipeline for today")
    parser.add_argument("--date", metavar="YYYY-MM-DD", help="Run the pipeline for a specific date")
    parser.add_argument("--analyze-only", action="store_true", help="Run analysis only (no plan generation or execution)")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without actually running Claude Code")
    parser.add_argument("--force", action="store_true", help="Re-analyze even dates that have already been analyzed")
    parser.add_argument("--auto-execute", action="store_true", help="Force plan execution to be enabled (overrides config.yaml)")
    parser.add_argument("--history", action="store_true", help="Show pipeline run history")
    parser.add_argument("--bot", action="store_true", help="Start the Discord bot only")
    args = parser.parse_args()

    if args.history:
        show_history(date=args.date)
        return

    if args.bot:
        from collector.bot import run
        run()
        return

    if args.analyze_only:
        from analyzer.analyzer import analyze_conversations
        date = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        files = _find_conversation_files(date)
        if not files:
            logger.error("No conversation files found for date %s.", date)
            sys.exit(1)
        result = analyze_conversations(files, date)
        logger.info("Analysis complete: %d topic(s) found", result.dev_topics_found)
        return

    if args.run or args.date:
        run_pipeline(
            date=args.date,
            dry_run=args.dry_run,
            force=args.force,
            auto_execute=True if args.auto_execute else None,
        )
        return

    # No arguments provided — fall back to interactive mode
    run_interactive()


if __name__ == "__main__":
    main()
