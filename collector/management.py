"""Discord bot commands for managing analysis results and plan files."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord
import yaml
from discord.ext import commands

from shared.embeds import (
    COLOR_FAILURE,
    COLOR_INFO,
    COLOR_SUCCESS,
    COLOR_WARNING,
    conversation_embed,
    plan_embed,
    run_history_embed,
    topic_embed,
)

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"

_TOPIC_STRUCTURE_PROMPT = """You are analyzing a manual development topic submission.
The user has described a development task they want to add to the pipeline.

User input:
{description}

Extract a structured development topic from this input.
Respond ONLY with valid JSON in this exact format:
{{
  "title": "concise English title (under 60 chars)",
  "category": "one of: feature, bug, refactor, infrastructure, discussion",
  "priority": "one of: high, medium, low",
  "summary": "2-3 sentence English summary of the task",
  "keywords": ["keyword1", "keyword2", "keyword3"],
  "actionable": true,
  "estimated_complexity": "one of: small, medium, large"
}}

IMPORTANT:
- ALL fields MUST be in English, even if the input is in Korean or another language.
- Always set actionable to true for manual submissions (the user explicitly wants this done).
- Be specific and accurate in the summary.
"""


def _parse_topic_response(response_text: str) -> dict | None:
    """Parse JSON from Claude's topic structuring response."""
    # Try direct parse
    try:
        return json.loads(response_text.strip())
    except json.JSONDecodeError:
        pass

    # Try markdown code fence
    fence_match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", response_text)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass

    # Scan for JSON object
    decoder = json.JSONDecoder()
    for i, ch in enumerate(response_text):
        if ch != "{":
            continue
        try:
            data, _ = decoder.raw_decode(response_text, i)
            if isinstance(data, dict) and "title" in data:
                return data
        except json.JSONDecodeError:
            continue

    return None


def _validate_date(date: str) -> bool:
    """Return True if date matches YYYY-MM-DD format."""
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}$", date))


def _load_json(path: Path) -> dict | list | None:
    """Load JSON from a file, returning None on error."""
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _save_json(path: Path, data: Any) -> None:
    """Write JSON to a file, creating parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


class ManagementCog(commands.Cog):
    """Cog for managing analysis results and plan files via Discord commands."""

    def __init__(self, bot: commands.Bot, data_dir: Path) -> None:
        self.bot = bot
        self.data_dir = data_dir

    # ── !topic ─────────────────────────────────────────────────────

    @commands.command(name="topic")
    async def topic_command(self, ctx: commands.Context, *, description: str = "") -> None:
        """Manually inject a development topic into the pipeline.

        Usage: !topic <description>
        Example: !topic Docker health check 추가. 현재 컨테이너 크래시 감지가 안됨
        """
        if not description.strip():
            await ctx.send(
                "**Usage:** `!topic <description>`\n"
                "Example: `!topic Add rate limiting to the API gateway`"
            )
            return

        await ctx.send("\u2699\uFE0F Structuring topic with Claude...")

        from shared.claude_cli import call_claude

        prompt = _TOPIC_STRUCTURE_PROMPT.format(description=description)

        loop = asyncio.get_running_loop()
        try:
            response = await loop.run_in_executor(None, lambda: call_claude(prompt, timeout=60))
        except Exception as e:
            await ctx.send(f"\u274C Failed to structure topic: {e}")
            return

        # Parse response
        topic_data = _parse_topic_response(response)
        if not topic_data:
            await ctx.send("\u274C Failed to parse Claude response. Please try again.")
            return

        # Add to analysis file
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        topic_id = self._inject_topic(date, topic_data, description)

        topic_data["id"] = topic_id
        embed = topic_embed(topic_data)
        embed.set_author(name=f"Manual Topic by {ctx.author.display_name}")
        embed.set_footer(text=f"Added to {date} analysis | Use !pipeline to generate plans")
        await ctx.send(embed=embed)

    def _inject_topic(self, date: str, topic_data: dict, original_desc: str) -> str:
        """Add a manually submitted topic to the analysis JSON file."""
        analysis_dir = self.data_dir / "analysis"
        analysis_dir.mkdir(parents=True, exist_ok=True)
        analysis_path = analysis_dir / f"{date}_analysis.json"

        existing = _load_json(analysis_path)
        if not isinstance(existing, dict):
            existing = {
                "date": date,
                "analyzed_at": datetime.now(timezone.utc).isoformat(),
                "source_files": [],
                "dev_topics": [],
                "total_messages_analyzed": 0,
                "dev_topics_found": 0,
            }

        topics = existing.get("dev_topics", [])
        topic_id = f"manual_{len(topics) + 1:03d}"

        topic_entry = {
            "id": topic_id,
            "title": topic_data.get("title", "Untitled"),
            "category": topic_data.get("category", "feature"),
            "priority": topic_data.get("priority", "medium"),
            "messages": [
                {
                    "author": "manual",
                    "content": original_desc,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            ],
            "summary": topic_data.get("summary", ""),
            "keywords": topic_data.get("keywords", []),
            "actionable": topic_data.get("actionable", True),
            "estimated_complexity": topic_data.get("estimated_complexity", "medium"),
            "source": "manual",
        }

        topics.append(topic_entry)
        existing["dev_topics"] = topics
        existing["dev_topics_found"] = len(topics)

        if "manual" not in existing.get("source_files", []):
            existing.setdefault("source_files", []).append("manual")

        _save_json(analysis_path, existing)
        logger.info("Manual topic injected: %s (id=%s)", topic_data.get("title"), topic_id)
        return topic_id

    # ── !conversations ─────────────────────────────────────────────

    @commands.command(name="conversations")
    async def conversations_command(self, ctx: commands.Context, date: str = "") -> None:
        """List conversation files, optionally filtered by date.

        Usage:
            !conversations          — list recent conversations
            !conversations 2026-03-01 — list conversations for a specific date
        """
        conv_dir = self.data_dir / "conversations"
        if not conv_dir.exists():
            await ctx.send("No conversations directory found.")
            return

        if date and not _validate_date(date):
            await ctx.send("Invalid date format. Use YYYY-MM-DD.")
            return

        pattern = f"{date}_*.json" if date else "*.json"
        files = sorted(conv_dir.glob(pattern), reverse=True)

        if not files:
            label = f"date `{date}`" if date else "any date"
            await ctx.send(f"No conversation files found for {label}.")
            return

        # Show up to 10 conversations as embeds
        for f in files[:10]:
            data = _load_json(f)
            if not data or not isinstance(data, dict):
                continue
            embed = conversation_embed(data, f.name)
            await ctx.send(embed=embed)

        if len(files) > 10:
            await ctx.send(f"... and {len(files) - 10} more conversation(s).")

    # ── !runs ──────────────────────────────────────────────────────

    @commands.command(name="runs")
    async def runs_command(self, ctx: commands.Context, date: str = "") -> None:
        """Show pipeline execution history.

        Usage:
            !runs              — show recent pipeline runs
            !runs 2026-03-01   — show runs for a specific date
        """
        if date and not _validate_date(date):
            await ctx.send("Invalid date format. Use YYYY-MM-DD.")
            return

        runs_dir = self.data_dir / "pipeline_runs"
        if not runs_dir.exists():
            await ctx.send("No pipeline run history found.")
            return

        pattern = f"{date}_*.json" if date else "*.json"
        run_files = sorted(runs_dir.glob(pattern), reverse=True)

        if not run_files:
            label = f"date `{date}`" if date else "any date"
            await ctx.send(f"No pipeline runs found for {label}.")
            return

        for rf in run_files[:10]:
            data = _load_json(rf)
            if not data or not isinstance(data, dict):
                continue
            embed = run_history_embed(data)
            await ctx.send(embed=embed)

        if len(run_files) > 10:
            await ctx.send(f"... and {len(run_files) - 10} more run(s).")

    # ── !analysis ──────────────────────────────────────────────────

    @commands.group(name="analysis", invoke_without_command=True)
    async def analysis_group(self, ctx: commands.Context) -> None:
        """Manage analysis results. Subcommands: list, show, reset."""
        if ctx.invoked_subcommand is None:
            await ctx.send(
                "**Analysis commands:**\n"
                "`!analysis list` \u2014 list all analyses\n"
                "`!analysis show <date>` \u2014 show topics for a date\n"
                "`!analysis reset <date>` \u2014 delete analysis (forces re-analysis)"
            )

    @analysis_group.command(name="list")
    async def analysis_list(self, ctx: commands.Context) -> None:
        """List all analysis files with summary info."""
        analysis_dir = self.data_dir / "analysis"
        if not analysis_dir.exists():
            await ctx.send("No analysis data found.")
            return

        files = sorted(analysis_dir.glob("*_analysis.json"), reverse=True)
        if not files:
            await ctx.send("No analysis files found.")
            return

        embed = discord.Embed(
            title=f"Analyses ({len(files)})",
            color=COLOR_INFO,
        )

        for f in files[:15]:
            data = _load_json(f)
            if not data or not isinstance(data, dict):
                embed.add_field(
                    name=f.name, value="(unreadable)", inline=False,
                )
                continue

            date = data.get("date", "?")
            topics_found = data.get("dev_topics_found", 0)
            total_msgs = data.get("total_messages_analyzed", 0)
            topics = data.get("dev_topics", [])
            actionable = sum(1 for t in topics if t.get("actionable", False))

            embed.add_field(
                name=f"\U0001F4CA {date}",
                value=f"{topics_found} topic(s) ({actionable} actionable), {total_msgs} msg(s)",
                inline=False,
            )

        await ctx.send(embed=embed)

    @analysis_group.command(name="show")
    async def analysis_show(self, ctx: commands.Context, date: str = "") -> None:
        """Show topics from a specific analysis date."""
        if not date:
            await ctx.send("Usage: `!analysis show <YYYY-MM-DD>`")
            return

        if not _validate_date(date):
            await ctx.send("Invalid date format. Use YYYY-MM-DD.")
            return

        analysis_path = self.data_dir / "analysis" / f"{date}_analysis.json"
        if not analysis_path.exists():
            await ctx.send(f"No analysis found for date `{date}`.")
            return

        data = _load_json(analysis_path)
        if not data or not isinstance(data, dict):
            await ctx.send(f"Failed to read analysis for `{date}`.")
            return

        topics = data.get("dev_topics", [])
        if not topics:
            await ctx.send(f"No topics in analysis for `{date}`.")
            return

        # Send each topic as an embed
        header = discord.Embed(
            title=f"Analysis: {date}",
            description=(
                f"Messages: {data.get('total_messages_analyzed', 0)} | "
                f"Topics: {len(topics)}"
            ),
            color=COLOR_INFO,
        )
        await ctx.send(embed=header)

        planned = _load_json(self.data_dir / "meta" / "planned.json") or {}
        for i, t in enumerate(topics[:15], 1):
            embed = topic_embed(t, index=i)
            is_planned = t.get("id", "") in planned
            if is_planned:
                embed.add_field(name="Planned", value="\U0001F4CB Yes", inline=True)
            await ctx.send(embed=embed)

    @analysis_group.command(name="reset")
    async def analysis_reset(self, ctx: commands.Context, date: str = "") -> None:
        """Delete analysis for a date to force re-analysis on next pipeline run."""
        if not date:
            await ctx.send("Usage: `!analysis reset <YYYY-MM-DD>`")
            return

        if not _validate_date(date):
            await ctx.send("Invalid date format. Use YYYY-MM-DD.")
            return

        analysis_path = self.data_dir / "analysis" / f"{date}_analysis.json"
        if not analysis_path.exists():
            await ctx.send(f"No analysis found for date `{date}`.")
            return

        analysis_path.unlink()
        await ctx.send(
            f"🗑️ Analysis for `{date}` deleted. "
            f"Next `!pipeline` run will re-analyze."
        )
        logger.info("Analysis reset for %s (by %s)", date, ctx.author)

    # ── !plan ──────────────────────────────────────────────────────

    @commands.group(name="plan", invoke_without_command=True)
    async def plan_group(self, ctx: commands.Context) -> None:
        """Manage plan files. Subcommands: list, show, execute, reset, delete."""
        if ctx.invoked_subcommand is None:
            await ctx.send(
                "**Plan commands:**\n"
                "`!plan list` — list all plans with status\n"
                "`!plan show <name>` — show plan summary\n"
                "`!plan execute <name>` — execute a plan with Claude Code\n"
                "`!plan reset <name>` — mark plan as not executed\n"
                "`!plan delete <name>` — delete plan file and metadata"
            )

    @plan_group.command(name="list")
    async def plan_list(self, ctx: commands.Context) -> None:
        """List all plan files with execution status."""
        plans_dir = self.data_dir / "plans"
        if not plans_dir.exists():
            await ctx.send("No plans directory found.")
            return

        plan_files = sorted(plans_dir.glob("*.md"), reverse=True)
        if not plan_files:
            await ctx.send("No plan files found.")
            return

        executed = _load_json(self.data_dir / "meta" / "executed.json") or {}

        embed = discord.Embed(
            title=f"Plans ({len(plan_files)})",
            color=COLOR_INFO,
        )

        for pf in plan_files[:20]:
            title = _extract_plan_title(pf)
            is_exec = pf.name in executed
            status = "\u2705" if is_exec else "\u23F3"

            exec_date = ""
            if is_exec:
                ts = (executed.get(pf.name) or {}).get("executed_at", "")[:10]
                if ts:
                    exec_date = f" ({ts})"

            embed.add_field(
                name=f"{status} {title}{exec_date}",
                value=f"`{pf.name}`",
                inline=False,
            )

        await ctx.send(embed=embed)

    @plan_group.command(name="show")
    async def plan_show(self, ctx: commands.Context, *, name: str = "") -> None:
        """Show summary of a specific plan file."""
        if not name:
            await ctx.send("Usage: `!plan show <filename>`")
            return

        pf = self._resolve_plan_file(name)
        if not pf:
            await ctx.send(f"Plan not found: `{name}`")
            return

        executed = _load_json(self.data_dir / "meta" / "executed.json") or {}
        is_exec = pf.name in executed
        exec_date = (executed.get(pf.name) or {}).get("executed_at", "")

        embed = plan_embed(pf, executed=is_exec, exec_date=exec_date)
        await ctx.send(embed=embed)

    @plan_group.command(name="execute")
    async def plan_execute(self, ctx: commands.Context, *, name: str = "") -> None:
        """Execute a specific plan file with Claude Code."""
        if not name:
            await ctx.send("Usage: `!plan execute <filename>`")
            return

        pf = self._resolve_plan_file(name)
        if not pf:
            await ctx.send(f"Plan not found: `{name}`")
            return

        executed = _load_json(self.data_dir / "meta" / "executed.json") or {}
        if pf.name in executed:
            await ctx.send(
                f"⚠️ `{pf.name}` already executed. "
                f"Use `!plan reset {pf.name}` first to re-execute."
            )
            return

        # Load executor config
        executor_cfg = self._load_executor_config()

        await ctx.send(f"⚙️ Executing: `{pf.name}` ...")

        from executor.executor import execute_plan

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: execute_plan(
                pf,
                data_dir=self.data_dir,
                claude_cli_path=executor_cfg.get("claude_cli_path", "claude"),
                timeout_seconds=executor_cfg.get("timeout_seconds", 300),
            ),
        )

        if result.success:
            await ctx.send(f"✅ Executed successfully: `{pf.name}`")
        else:
            excerpt = result.error[:500] if result.error else "Unknown error"
            await ctx.send(
                f"❌ Execution failed: `{pf.name}`\n```\n{excerpt}\n```"
            )

    @plan_group.command(name="reset")
    async def plan_reset(self, ctx: commands.Context, *, name: str = "") -> None:
        """Remove executed status from a plan (allows re-execution)."""
        if not name:
            await ctx.send("Usage: `!plan reset <filename>`")
            return

        pf = self._resolve_plan_file(name)
        if not pf:
            await ctx.send(f"Plan not found: `{name}`")
            return

        manifest_path = self.data_dir / "meta" / "executed.json"
        manifest = _load_json(manifest_path)
        if not isinstance(manifest, dict) or pf.name not in manifest:
            await ctx.send(f"`{pf.name}` is not marked as executed.")
            return

        del manifest[pf.name]
        _save_json(manifest_path, manifest)

        await ctx.send(
            f"🔄 `{pf.name}` reset to pending. "
            f"Re-execute with `!plan execute {pf.name}`."
        )
        logger.info("Plan reset: %s (by %s)", pf.name, ctx.author)

    @plan_group.command(name="delete")
    async def plan_delete(self, ctx: commands.Context, *, name: str = "") -> None:
        """Delete a plan file and clean up related metadata."""
        if not name:
            await ctx.send("Usage: `!plan delete <filename>`")
            return

        pf = self._resolve_plan_file(name)
        if not pf:
            await ctx.send(f"Plan not found: `{name}`")
            return

        # Clean executed manifest
        exec_path = self.data_dir / "meta" / "executed.json"
        exec_data = _load_json(exec_path)
        if isinstance(exec_data, dict) and pf.name in exec_data:
            del exec_data[pf.name]
            _save_json(exec_path, exec_data)

        # Clean planned manifest
        planned_path = self.data_dir / "meta" / "planned.json"
        planned_data = _load_json(planned_path)
        if isinstance(planned_data, dict):
            to_remove = [
                k for k, v in planned_data.items()
                if v.get("plan_file") == pf.name
            ]
            for k in to_remove:
                del planned_data[k]
            if to_remove:
                _save_json(planned_path, planned_data)

        pf.unlink()
        await ctx.send(f"🗑️ Deleted: `{pf.name}`")
        logger.info("Plan deleted: %s (by %s)", pf.name, ctx.author)

    # ── Helpers ────────────────────────────────────────────────────

    def _resolve_plan_file(self, name: str) -> Path | None:
        """Find a plan file by exact name, with .md, or partial match."""
        plans_dir = self.data_dir / "plans"
        if not plans_dir.exists():
            return None

        name_md = name if name.endswith(".md") else name + ".md"

        # Exact match
        exact = plans_dir / name_md
        if exact.exists():
            resolved = exact
            if not resolved.resolve().is_relative_to(plans_dir.resolve()):
                return None
            return resolved

        # Partial match (unique substring)
        matches = [
            p for p in plans_dir.glob("*.md")
            if name.lower() in p.name.lower()
        ]
        if len(matches) == 1:
            resolved = matches[0]
            if not resolved.resolve().is_relative_to(plans_dir.resolve()):
                return None
            return resolved

        return None

    def _load_executor_config(self) -> dict:
        """Load executor section from config.yaml."""
        if _CONFIG_PATH.exists():
            try:
                with _CONFIG_PATH.open(encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
                return cfg.get("executor", {})
            except (yaml.YAMLError, OSError):
                pass
        return {}


def _extract_plan_title(plan_file: Path) -> str:
    """Extract the plan title from the markdown heading."""
    try:
        content = plan_file.read_text(encoding="utf-8")
        match = re.search(r"^# Plan:\s*(.+)$", content, re.MULTILINE)
        return match.group(1).strip() if match else plan_file.stem
    except OSError:
        return plan_file.stem


def _truncate(text: str, limit: int = 1900) -> str:
    """Truncate text to fit within Discord's message limit."""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n..."
