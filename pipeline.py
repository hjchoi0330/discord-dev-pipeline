#!/usr/bin/env python3
"""Discord Dev Pipeline - 메인 오케스트레이터.

데이터 흐름:
  Discord 대화 수집 → 개발 토픽 분석 → 계획 생성 → Claude Code 실행
"""

from __future__ import annotations

import argparse
import json as _json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import os

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
    """날짜에 해당하는 대화 파일을 탐색합니다.

    날짜 경계를 넘는 대화를 놓치지 않기 위해 전날 파일도 함께 포함합니다.
    (예: 23시에 시작된 녹음이 다음 날 01시에 종료된 경우)
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
    """전체 파이프라인을 실행합니다.

    각 stage의 진행 상태를 PipelineRun으로 추적하여
    data/pipeline_runs/ 에 이력을 저장합니다.

    Args:
        date: 분석할 날짜 (YYYY-MM-DD). None이면 오늘 날짜.
        dry_run: True이면 Claude Code를 실제로 실행하지 않음.
        force: True이면 이미 분석된 날짜도 재분석.
        auto_execute: True이면 계획 실행을 강제 활성화.
            None이면 config.yaml의 pipeline.auto_execute 값을 사용.
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
    logger.info("=== Discord Dev Pipeline 시작 (날짜: %s, run: %s) ===", date, run.run_id)

    try:
        # ── Stage 1: Collect ─────────────────────────────────────────
        collect_stage = run.get_stage(STAGE_COLLECT)
        collect_stage.start()

        conv_files = _find_conversation_files(date)
        if not conv_files:
            collect_stage.fail("날짜에 해당하는 대화 파일 없음")
            logger.warning("날짜 %s에 해당하는 대화 파일이 없습니다.", date)
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
                    logger.info("모든 대화 파일이 이미 분석되었습니다. 캐시된 분석을 사용합니다.")
                    analysis = _load_existing_analysis(date)
                    if analysis:
                        analyze_stage.complete({
                            "source": "cached",
                            "topics_found": analysis.dev_topics_found,
                        })
                    else:
                        analyze_stage.fail("캐시된 분석 파일 로드 실패")
                else:
                    skipped = len(conv_files) - len(new_files)
                    if skipped > 0:
                        logger.info("이미 분석된 파일 %d개 스킵, 새 파일 %d개 분석 예정", skipped, len(new_files))
                    conv_files = new_files

        if analysis is None and analyze_stage.status != "failed":
            logger.info("분석할 대화 파일 %d개:", len(conv_files))
            for f in conv_files:
                logger.info("  - %s", f)
            logger.info("대화 파일 분석 중...")
            analysis = analyze_conversations(conv_files, date, data_dir=_DATA_DIR)
            analyze_stage.complete({
                "source": "fresh",
                "topics_found": analysis.dev_topics_found,
                "messages_analyzed": analysis.total_messages_analyzed,
            })

        if analysis is None or not analysis.dev_topics:
            if analyze_stage.status != "failed":
                logger.info("개발 관련 토픽이 발견되지 않았습니다.")
            run.get_stage(STAGE_PLAN).skip("No dev topics found")
            run.get_stage(STAGE_EXECUTE).skip("No dev topics found")
            return

        logger.info("발견된 개발 토픽 %d개:", analysis.dev_topics_found)
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
            logger.info("생성된 계획 파일 %d개:", len(plan_files))
            for p in plan_files:
                logger.info("  - %s", p)

        # ── Stage 4: Execute ─────────────────────────────────────────
        execute_stage = run.get_stage(STAGE_EXECUTE)

        if not plan_files:
            execute_stage.skip("No new plans to execute")
            logger.info("실행할 새 계획이 없습니다.")
        elif dry_run:
            execute_stage.skip("Dry run mode")
            logger.info("[DRY RUN] 계획 실행을 건너뜁니다.")
        elif not (auto_execute if auto_execute is not None else pipeline_cfg.get("auto_execute", False)):
            execute_stage.skip("auto_execute disabled")
            logger.info("자동 실행이 비활성화 상태입니다. config.yaml의 pipeline.auto_execute를 true로 설정하세요.")
        else:
            execute_stage.start({"plans_to_execute": len(plan_files)})
            logger.info("Claude Code로 계획 실행 중...")
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
    """파이프라인 실행 결과를 stage별로 출력합니다."""
    logger.info("=== Pipeline Run 요약 (%s) ===", run.run_id)
    logger.info("날짜: %s | 상태: %s", run.date, run.status)
    duration = run.duration_seconds()
    if duration is not None:
        logger.info("총 소요 시간: %.1f초", duration)
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


def _print_summary(results) -> None:
    """이전 버전과의 호환을 위해 유지합니다."""
    succeeded = sum(1 for r in results if r.success)
    logger.info("=== 실행 결과 요약 ===")
    logger.info("성공: %d / 전체: %d", succeeded, len(results))
    for r in results:
        status = "O" if r.success else "X"
        logger.info("  [%s] %s", status, Path(r.plan_file).name)
        if not r.success and r.error:
            logger.error("      오류: %s", r.error[:200])


def show_history(date: str | None = None) -> None:
    """파이프라인 실행 이력을 출력합니다."""
    runs = load_runs(_DATA_DIR, date=date)
    if not runs:
        label = f"날짜 {date}" if date else "전체"
        print(f"\n{label} 파이프라인 실행 이력이 없습니다.")
        return

    print(f"\n=== Pipeline 실행 이력 ({len(runs)}건) ===")
    for run in runs:
        print(f"\n{run.summary_line()}")
        duration = run.duration_seconds()
        if duration is not None:
            print(f"  소요: {duration:.1f}초")
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
    """대화형 CLI 메뉴."""
    from executor.executor import execute_plan

    print("\n=== Discord Dev Pipeline ===")
    while True:
        print("\n메뉴:")
        print("  1. 오늘 날짜로 전체 파이프라인 실행")
        print("  2. 특정 날짜로 파이프라인 실행")
        print("  3. 분석만 실행 (계획 생성 없음)")
        print("  4. 기존 계획 파일 목록 보기")
        print("  5. 특정 계획 파일 실행")
        print("  6. 파이프라인 실행 이력 보기")
        print("  7. Discord 봇 시작")
        print("  0. 종료")

        choice = input("\n선택: ").strip()

        if choice == "1":
            run_pipeline()
        elif choice == "2":
            date = input("날짜 입력 (YYYY-MM-DD): ").strip()
            run_pipeline(date=date)
        elif choice == "3":
            from analyzer.analyzer import analyze_conversations
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            files = _find_conversation_files(date)
            if not files:
                print(f"날짜 {date}에 대화 파일이 없습니다.")
            else:
                result = analyze_conversations(files, date)
                print(f"\n분석 완료: {result.dev_topics_found}개 토픽 발견")
        elif choice == "4":
            plan_dir = _DATA_DIR / "plans"
            if plan_dir.exists():
                plans = sorted(plan_dir.glob("*.md"))
                if plans:
                    print(f"\n계획 파일 {len(plans)}개:")
                    for i, p in enumerate(plans, 1):
                        print(f"  {i}. {p.name}")
                else:
                    print("계획 파일이 없습니다.")
            else:
                print("계획 디렉토리가 없습니다.")
        elif choice == "5":
            plan_dir = _DATA_DIR / "plans"
            plans = sorted(plan_dir.glob("*.md")) if plan_dir.exists() else []
            if not plans:
                print("계획 파일이 없습니다.")
            else:
                print(f"\n계획 파일 {len(plans)}개:")
                for i, p in enumerate(plans, 1):
                    print(f"  {i}. {p.name}")
                idx = input("\n실행할 번호 (0=취소): ").strip()
                if idx.isdigit() and 1 <= int(idx) <= len(plans):
                    plan = plans[int(idx) - 1]
                    print(f"\n계획 실행 중: {plan.name}")
                    result = execute_plan(plan, data_dir=_DATA_DIR)
                    status = "성공" if result.success else f"실패: {result.error[:200]}"
                    print(f"결과: {status}")
                elif idx != "0":
                    print("잘못된 번호입니다.")
        elif choice == "6":
            date_input = input("날짜 필터 (YYYY-MM-DD, 빈 값=전체): ").strip() or None
            show_history(date=date_input)
        elif choice == "7":
            from collector.bot import run
            print("Discord 봇을 시작합니다... (Ctrl+C로 종료)")
            run()
        elif choice == "0":
            print("종료합니다.")
            break
        else:
            print("잘못된 선택입니다.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Discord Dev Pipeline - Discord 대화에서 개발 계획을 자동으로 생성하고 실행합니다."
    )
    parser.add_argument("--run", action="store_true", help="오늘 날짜로 파이프라인 실행")
    parser.add_argument("--date", metavar="YYYY-MM-DD", help="특정 날짜로 파이프라인 실행")
    parser.add_argument("--analyze-only", action="store_true", help="분석만 실행 (계획/실행 없음)")
    parser.add_argument("--dry-run", action="store_true", help="실제 Claude Code 실행 없이 시뮬레이션")
    parser.add_argument("--force", action="store_true", help="이미 분석된 날짜도 재분석")
    parser.add_argument("--auto-execute", action="store_true", help="계획 실행을 강제 활성화 (config.yaml 무시)")
    parser.add_argument("--history", action="store_true", help="파이프라인 실행 이력 조회")
    parser.add_argument("--bot", action="store_true", help="Discord 봇만 시작")
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
            logger.error("날짜 %s에 대화 파일이 없습니다.", date)
            sys.exit(1)
        result = analyze_conversations(files, date)
        logger.info("분석 완료: %d개 토픽 발견", result.dev_topics_found)
        return

    if args.run or args.date:
        run_pipeline(
            date=args.date,
            dry_run=args.dry_run,
            force=args.force,
            auto_execute=True if args.auto_execute else None,
        )
        return

    # 인수 없으면 대화형 모드
    run_interactive()


if __name__ == "__main__":
    main()
