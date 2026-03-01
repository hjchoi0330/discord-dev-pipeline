"""Generate Claude Code execution plan files from analyzed dev topics."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from analyzer.analyzer import AnalysisResult, DevTopic
from shared.claude_cli import call_claude as _call_claude
from shared.plan_format import PLAN_PROMPT_HEADING

logger = logging.getLogger(__name__)

_PLAN_PROMPT = """You are a senior software architect creating a detailed implementation plan.

Based on the following development topic from a Discord conversation, create a comprehensive plan
that a developer (or Claude Code AI) can follow to implement the feature/fix.

Topic Information:
- Title: {title}
- Category: {category}
- Priority: {priority}
- Complexity: {complexity}
- Summary: {summary}
- Keywords: {keywords}

Discord Discussion (relevant messages):
{messages}

Create a detailed implementation plan in the following EXACT markdown format:

# Plan: {title}
**Date:** {date}
**Category:** {category}
**Priority:** {priority}
**Complexity:** {complexity}
**Source:** Discord conversation

## Context
[2-3 sentences explaining what was discussed and why this is needed]

## Objective
[One clear, concise sentence stating the goal]

## Requirements
- [Specific requirement 1]
- [Specific requirement 2]
- [Add more as needed]

## Technical Specifications
### Tech Stack
- [Primary language/framework]
- [Libraries/tools needed]

### Architecture
[Brief description of how this fits into the existing system or how to structure it]

## Implementation Steps
1. [Concrete step 1]
2. [Concrete step 2]
3. [Continue with all necessary steps]

## Acceptance Criteria
- [ ] [Verifiable criterion 1]
- [ ] [Verifiable criterion 2]
- [ ] [Add more as needed]

{prompt_heading}
```
Implement the following {category}:

{title}

Context: {summary}

[Write a comprehensive, self-contained prompt that Claude Code can use to implement this.
Include all necessary context, requirements, and technical details.
Be specific about file names, function signatures, and expected behavior.
The prompt should be complete enough that no additional information is needed.]
```

IMPORTANT:
- Write the ENTIRE plan in English, including all headings, descriptions, and the Claude Code Prompt.
- The title "{title}" may NOT be in English. You MUST translate it to a clear, concise English title
  and use the translated English title in the "# Plan:" heading.
- Even if the Discord discussion was in Korean or another language, translate everything into clear English.
- The "Claude Code Prompt" section MUST contain a complete, standalone prompt
  that starts with "Implement" and includes all context needed for implementation.
"""


def _sanitize_filename(title: str) -> str:
    """Convert a title to a safe ASCII-only filename slug."""
    sanitized = re.sub(r"[^a-zA-Z0-9\s-]", "", title.lower())
    sanitized = re.sub(r"[\s_-]+", "-", sanitized).strip("-")
    return sanitized[:60] if sanitized else "untitled"


def _format_messages(topic: DevTopic) -> str:
    if not topic.messages:
        return "(no specific messages captured)"
    lines = []
    for msg in topic.messages[:20]:  # 최대 20개 메시지만 포함
        ts = msg.timestamp[:19].replace("T", " ") if msg.timestamp else ""
        lines.append(f"- {msg.author} ({ts}): {msg.content}")
    return "\n".join(lines)


def _load_planned_manifest(data_dir: Path) -> dict[str, Any]:
    """Load the planned-topics manifest from data/meta/planned.json."""
    manifest_path = data_dir / "meta" / "planned.json"
    if not manifest_path.exists():
        return {}
    try:
        with manifest_path.open(encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _mark_topic_planned(
    topic: DevTopic, plan_file: Path, data_dir: Path,
) -> None:
    """Record a topic as planned in data/meta/planned.json."""
    manifest_path = data_dir / "meta" / "planned.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = _load_planned_manifest(data_dir)
    manifest[topic.id] = {
        "title": topic.title,
        "plan_file": plan_file.name,
        "planned_at": datetime.now(timezone.utc).isoformat(),
    }
    with manifest_path.open("w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)


def generate_plans(
    analysis: AnalysisResult, output_dir: Path, *, data_dir: Path | None = None,
) -> list[Path]:
    """Generate markdown plan files from analyzed dev topics.

    Args:
        analysis: Analysis result containing dev_topics.
        output_dir: Directory to write plan files into.
        data_dir: Root data directory for metadata (defaults to output_dir.parent).

    Returns:
        List of generated plan file paths (sorted by priority).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    if data_dir is None:
        data_dir = output_dir.parent

    # actionable topics only, sorted by priority
    priority_order = {"high": 0, "medium": 1, "low": 2}
    actionable_topics = [t for t in analysis.dev_topics if t.actionable]
    actionable_topics.sort(key=lambda t: priority_order.get(t.priority, 99))

    if not actionable_topics:
        logger.info("실행 가능한 개발 토픽이 없습니다.")
        return []

    manifest = _load_planned_manifest(data_dir)
    generated: list[Path] = []
    date = analysis.date

    for topic in actionable_topics:
        if topic.id in manifest:
            logger.info(
                "Skipping topic '%s' (id=%s) — already planned: %s",
                topic.title, topic.id, manifest[topic.id].get("plan_file", "?"),
            )
            continue

        messages_text = _format_messages(topic)
        prompt = _PLAN_PROMPT.format(
            title=topic.title,
            category=topic.category,
            priority=topic.priority,
            complexity=topic.estimated_complexity,
            summary=topic.summary,
            keywords=", ".join(topic.keywords),
            messages=messages_text,
            date=date,
            prompt_heading=PLAN_PROMPT_HEADING,
        )

        try:
            plan_content = _call_claude(prompt)
        except Exception as e:
            logger.error("계획 생성 실패 (토픽: %s): %s", topic.title, e)
            continue

        slug = _sanitize_filename(topic.title)
        filename = f"{date}_{slug}.md"
        output_path = output_dir / filename
        output_path.write_text(plan_content, encoding="utf-8")
        _mark_topic_planned(topic, output_path, data_dir)

        logger.info("계획 생성: %s", output_path)
        generated.append(output_path)

    return generated
