"""Reusable Discord embed builders for pipeline results, topics, and plans."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord

# Color constants
COLOR_SUCCESS = 0x2ECC71   # green
COLOR_FAILURE = 0xE74C3C   # red
COLOR_WARNING = 0xF39C12   # yellow/orange
COLOR_INFO = 0x3498DB      # blue
COLOR_NEUTRAL = 0x95A5A6   # grey

_STAGE_EMOJI = {
    "completed": "\u2705",  # white check mark
    "failed": "\u274C",     # cross mark
    "skipped": "\u23ED\uFE0F",  # next track
    "running": "\u23F3",    # hourglass
    "pending": "\u2B1C",    # white square
}

_PRIORITY_EMOJI = {
    "high": "\U0001F534",    # red circle
    "medium": "\U0001F7E1",  # yellow circle
    "low": "\U0001F7E2",     # green circle
}


def pipeline_result_embed(
    run_data: dict[str, Any],
    stages: list[dict[str, Any]],
) -> discord.Embed:
    """Build an embed summarizing a pipeline run."""
    status = run_data.get("status", "unknown")
    date = run_data.get("date", "?")

    if status == "completed":
        color = COLOR_SUCCESS
        title = f"Pipeline Complete — {date}"
    elif status == "failed":
        color = COLOR_FAILURE
        title = f"Pipeline Failed — {date}"
    else:
        color = COLOR_WARNING
        title = f"Pipeline Partial — {date}"

    embed = discord.Embed(title=title, color=color, timestamp=datetime.now(timezone.utc))

    stage_lines = []
    for s in stages:
        emoji = _STAGE_EMOJI.get(s.get("status", "pending"), "\u2753")
        name = s.get("name", "?")
        dur = s.get("duration")
        dur_str = f" ({dur:.1f}s)" if dur is not None else ""
        detail = s.get("detail", "")
        detail_str = f" — {detail}" if detail else ""
        stage_lines.append(f"{emoji} **{name}**{dur_str}{detail_str}")

    embed.add_field(name="Stages", value="\n".join(stage_lines) or "No stages", inline=False)

    duration = run_data.get("duration")
    if duration is not None:
        embed.set_footer(text=f"Total: {duration:.1f}s")

    return embed


def topic_embed(topic: dict[str, Any], index: int | None = None) -> discord.Embed:
    """Build an embed for a single development topic."""
    title = topic.get("title", "Untitled")
    if index is not None:
        title = f"#{index} {title}"

    priority = topic.get("priority", "medium")
    color = {
        "high": COLOR_FAILURE,
        "medium": COLOR_WARNING,
        "low": COLOR_SUCCESS,
    }.get(priority, COLOR_INFO)

    embed = discord.Embed(title=title, color=color)

    pri_emoji = _PRIORITY_EMOJI.get(priority, "")
    actionable = "\u2705" if topic.get("actionable") else "\u274C"
    embed.add_field(name="Priority", value=f"{pri_emoji} {priority.upper()}", inline=True)
    embed.add_field(name="Category", value=topic.get("category", "?"), inline=True)
    embed.add_field(name="Actionable", value=actionable, inline=True)

    if topic.get("summary"):
        embed.add_field(name="Summary", value=topic["summary"][:1024], inline=False)

    if topic.get("keywords"):
        kw = ", ".join(topic["keywords"][:10])
        embed.add_field(name="Keywords", value=f"`{kw}`", inline=False)

    complexity = topic.get("estimated_complexity", "")
    if complexity:
        embed.set_footer(text=f"Complexity: {complexity}")

    return embed


def plan_embed(plan_file: Path, executed: bool = False, exec_date: str = "") -> discord.Embed:
    """Build an embed for a plan file summary."""
    try:
        content = plan_file.read_text(encoding="utf-8")
    except OSError:
        return discord.Embed(
            title=plan_file.stem,
            description="Failed to read plan file.",
            color=COLOR_FAILURE,
        )

    title_match = re.search(r"^# Plan:\s*(.+)$", content, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else plan_file.stem

    color = COLOR_SUCCESS if executed else COLOR_INFO
    embed = discord.Embed(title=title, color=color)

    # Extract metadata
    for label in ("Category", "Priority", "Complexity"):
        match = re.search(rf"\*\*{label}:\*\*\s*(.+)", content)
        if match:
            embed.add_field(name=label, value=match.group(1).strip(), inline=True)

    # Objective
    obj_match = re.search(r"## Objective\n(.+?)(?=\n##|\Z)", content, re.DOTALL)
    if obj_match:
        embed.add_field(name="Objective", value=obj_match.group(1).strip()[:1024], inline=False)

    status_str = "\u2705 Executed"
    if executed and exec_date:
        status_str += f" ({exec_date[:10]})"
    elif not executed:
        status_str = "\u23F3 Pending"
    embed.add_field(name="Status", value=status_str, inline=True)

    embed.set_footer(text=plan_file.name)
    return embed


def conversation_embed(conv_data: dict[str, Any], filename: str) -> discord.Embed:
    """Build an embed for a conversation file summary."""
    date = conv_data.get("date", "?")
    channel = conv_data.get("channel", "?")
    guild = conv_data.get("guild", "?")
    messages = conv_data.get("messages", [])
    conv_type = conv_data.get("type", "unknown")

    authors = set(m.get("author", "?") for m in messages)

    embed = discord.Embed(
        title=f"{guild} / #{channel}",
        color=COLOR_INFO,
    )
    embed.add_field(name="Date", value=date, inline=True)
    embed.add_field(name="Messages", value=str(len(messages)), inline=True)
    embed.add_field(name="Speakers", value=str(len(authors)), inline=True)
    embed.add_field(name="Type", value=conv_type, inline=True)

    if messages:
        start = messages[0].get("timestamp", "")[:19].replace("T", " ")
        end = messages[-1].get("timestamp", "")[:19].replace("T", " ")
        if start:
            embed.add_field(name="Time", value=f"{start} ~ {end}", inline=False)

    embed.set_footer(text=filename)
    return embed


def run_history_embed(run: dict[str, Any]) -> discord.Embed:
    """Build an embed for a single pipeline run history entry."""
    status = run.get("status", "unknown")
    date = run.get("date", "?")
    run_id = run.get("run_id", "?")

    color = {
        "completed": COLOR_SUCCESS,
        "failed": COLOR_FAILURE,
        "partial": COLOR_WARNING,
    }.get(status, COLOR_NEUTRAL)

    embed = discord.Embed(
        title=f"{date} — {status.upper()}",
        color=color,
    )

    stages = run.get("stages", [])
    stage_lines = []
    for s in stages:
        emoji = _STAGE_EMOJI.get(s.get("status", "pending"), "\u2753")
        name = s.get("name", "?")
        dur = None
        if s.get("started_at") and s.get("completed_at"):
            try:
                start = datetime.fromisoformat(s["started_at"])
                end = datetime.fromisoformat(s["completed_at"])
                dur = (end - start).total_seconds()
            except (ValueError, TypeError):
                pass
        dur_str = f" ({dur:.1f}s)" if dur is not None else ""

        # Extract useful detail from output_summary
        summary = s.get("output_summary", {})
        detail_parts = []
        for key in ("topics_found", "plans_generated", "succeeded", "files_found"):
            if key in summary:
                detail_parts.append(f"{key}={summary[key]}")
        detail_str = f" — {', '.join(detail_parts)}" if detail_parts else ""

        error = s.get("error", "")
        if error and s.get("status") == "failed":
            detail_str += f" \u274C {error[:80]}"

        stage_lines.append(f"{emoji} **{name}**{dur_str}{detail_str}")

    embed.add_field(name="Stages", value="\n".join(stage_lines) or "No stages", inline=False)

    duration = run.get("duration")
    if duration is None:
        started = run.get("started_at", "")
        completed = run.get("completed_at", "")
        if started and completed:
            try:
                duration = (
                    datetime.fromisoformat(completed) - datetime.fromisoformat(started)
                ).total_seconds()
            except (ValueError, TypeError):
                pass

    footer = f"Run: {run_id}"
    if duration is not None:
        footer += f" | {duration:.1f}s"
    embed.set_footer(text=footer)

    return embed
