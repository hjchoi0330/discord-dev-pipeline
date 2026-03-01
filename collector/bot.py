"""Discord voice channel recording bot - transcribes voice conversations with Whisper and saves them to files."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord
from discord.ext import commands

# Load libopus from macOS Homebrew (required for voice decoding)
if not discord.opus.is_loaded():
    try:
        discord.opus.load_opus("/opt/homebrew/lib/libopus.dylib")
    except OSError:
        pass  # On other OS, auto-loaded from system default path

from collector.config import CollectorConfig, load_config
from shared.claude_cli import clean_env as _clean_claude_env

_LOG_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
_LOG_DIR = Path(__file__).parent.parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format=_LOG_FMT,
    datefmt=_LOG_DATEFMT,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_LOG_DIR / "bot.log", encoding="utf-8"),
    ],
    force=True,  # Override pipeline.py's basicConfig when imported via --bot
)
logger = logging.getLogger(__name__)

_file_locks: dict[str, threading.Lock] = {}
_locks_mutex = threading.Lock()


def _get_file_lock(path: str) -> threading.Lock:
    with _locks_mutex:
        if path not in _file_locks:
            _file_locks[path] = threading.Lock()
        return _file_locks[path]


@dataclass
class RecordingSession:
    guild_id: int
    channel_id: int
    channel_name: str
    guild_name: str
    started_at: str
    voice_client: discord.VoiceClient


def _transcribe_audio(audio_bytes: bytes, language: str = "ko", config: CollectorConfig | None = None) -> str:
    """Transcribes audio bytes to text using faster-whisper."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        logger.error("faster-whisper is not installed. Run pip install faster-whisper and try again.")
        return ""

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    try:
        # Model is cached for the lifetime of the process (downloaded on first call)
        model = _get_whisper_model(config)
        segments, info = model.transcribe(
            tmp_path,
            language=language,
            beam_size=5,
            vad_filter=True,  # Automatically remove silent segments
            vad_parameters={"min_silence_duration_ms": 500},
        )
        text = " ".join(seg.text.strip() for seg in segments if seg.text.strip())
        logger.debug("Transcription result (%.1fs): %s", info.duration, text[:100])
        return text
    finally:
        Path(tmp_path).unlink(missing_ok=True)


_whisper_model = None
_whisper_lock = threading.Lock()


def _get_whisper_model(config: CollectorConfig | None = None):
    """Whisper model singleton (thread-safe)."""
    global _whisper_model
    if _whisper_model is None:
        with _whisper_lock:
            if _whisper_model is None:
                from faster_whisper import WhisperModel
                model_size = config.whisper_model if config else "small"
                device = config.whisper_device if config else "cpu"
                compute_type = config.whisper_compute_type if config else "int8"
                logger.info("Loading Whisper model (%s, %s)...", model_size, device)
                _whisper_model = WhisperModel(
                    model_size,
                    device=device,
                    compute_type=compute_type,
                )
                logger.info("Whisper model ready")
    return _whisper_model


def _save_transcription(
    data_dir: str,
    guild_name: str,
    channel_name: str,
    session_start: str,
    session_end: str,
    messages: list[dict[str, Any]],
) -> Path:
    """Saves transcription results to a JSON file."""
    date_str = session_start[:10]
    safe_guild = "".join(c if c.isalnum() or c in "-_" else "_" for c in guild_name)
    safe_channel = "".join(c if c.isalnum() or c in "-_" else "_" for c in channel_name)
    filename = f"{date_str}_{safe_guild}_{safe_channel}_voice.json"
    path = Path(data_dir) / filename
    path.parent.mkdir(parents=True, exist_ok=True)

    lock = _get_file_lock(str(path))
    with lock:
        # Append to existing file if present (supports multiple sessions on the same day)
        existing_messages: list[dict] = []
        if path.exists():
            try:
                with path.open(encoding="utf-8") as f:
                    existing = json.load(f)
                    existing_messages = existing.get("messages", [])
            except (json.JSONDecodeError, OSError):
                pass

        data = {
            "date": date_str,
            "channel": channel_name,
            "guild": guild_name,
            "type": "voice_transcription",
            "session_start": session_start,
            "session_end": session_end,
            "messages": existing_messages + messages,
        }
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    logger.info("Transcription saved: %s (%d utterances)", path, len(messages))
    return path


class VoiceCog(commands.Cog):
    """Cog for voice recording and pipeline commands."""

    def __init__(self, bot: commands.Bot, config: CollectorConfig) -> None:
        self.bot = bot
        self.config = config
        # guild_id → RecordingSession
        self._sessions: dict[int, RecordingSession] = {}

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        logger.info("Bot logged in: %s (id=%s)", self.bot.user, self.bot.user.id)  # type: ignore[union-attr]
        logger.info("Voice channel recording mode active. Use '!join [channel name]' to connect to a voice channel.")

    # ── Voice channel commands ────────────────────────────────────────────

    @commands.command(name="join")
    async def join_command(self, ctx: commands.Context, *, channel_name: str = "") -> None:
        """Connects to a voice channel and starts recording.

        Usage: !join [channel name]  (omit channel name to join the user's current channel)
        """
        if not isinstance(ctx.guild, discord.Guild):
            await ctx.send("This command must be used inside a server.")
            return

        # Find channel: explicit name > user's current channel
        target_channel: discord.VoiceChannel | None = None
        if channel_name:
            target_channel = discord.utils.get(ctx.guild.voice_channels, name=channel_name)
            if not target_channel:
                await ctx.send(f"Voice channel '{channel_name}' not found.")
                return
        elif isinstance(ctx.author, discord.Member) and ctx.author.voice:
            target_channel = ctx.author.voice.channel  # type: ignore[assignment]
        else:
            await ctx.send("Please specify a channel name or join a voice channel first.")
            return

        guild_id = ctx.guild.id
        if guild_id in self._sessions:
            await ctx.send("Already recording. Use '!leave' to stop first.")
            return

        try:
            vc = await target_channel.connect()
        except discord.ClientException as e:
            await ctx.send(f"Failed to connect to channel: {e}")
            return

        session = RecordingSession(
            guild_id=guild_id,
            channel_id=target_channel.id,
            channel_name=target_channel.name,
            guild_name=ctx.guild.name,
            started_at=datetime.now(timezone.utc).isoformat(),
            voice_client=vc,
        )
        self._sessions[guild_id] = session

        # Start recording with discord.py sinks
        vc.start_recording(
            discord.sinks.WaveSink(),
            self._recording_finished_callback,
            ctx.channel,  # Text channel to send results to when done
            session,
        )

        await ctx.send(
            f"Recording started in **#{target_channel.name}**. "
            f"Type `!leave` to stop."
        )
        logger.info("[%s] Recording started: #%s", ctx.guild.name, target_channel.name)

    @commands.command(name="leave")
    async def leave_command(self, ctx: commands.Context) -> None:
        """Stops recording and leaves the voice channel.

        Usage: !leave
        """
        if not isinstance(ctx.guild, discord.Guild):
            await ctx.send("This command must be used inside a server.")
            return

        guild_id = ctx.guild.id
        if guild_id not in self._sessions:
            await ctx.send("No active recording session.")
            return

        session = self._sessions[guild_id]
        await ctx.send("Stopping recording. Transcription in progress...")

        # stop_recording triggers _recording_finished_callback
        session.voice_client.stop_recording()

    @commands.command(name="status")
    async def status_command(self, ctx: commands.Context) -> None:
        """Checks the current recording status."""
        if not isinstance(ctx.guild, discord.Guild):
            return
        session = self._sessions.get(ctx.guild.id)
        if session:
            elapsed = datetime.now(timezone.utc).timestamp() - datetime.fromisoformat(
                session.started_at
            ).timestamp()
            await ctx.send(
                f"Recording: **#{session.channel_name}** "
                f"(elapsed {int(elapsed // 60)}m {int(elapsed % 60)}s)"
            )
        else:
            await ctx.send("No active recording session.")

    async def _run_pipeline_subprocess(self, auto_execute: bool = True) -> tuple[bool, str]:
        """Run the pipeline as a subprocess and return (success, message).

        Pipeline logs go to stderr (via Python logging). We forward each
        line to the bot logger and also extract summary info from them.

        Args:
            auto_execute: Whether to force-enable plan execution.
                Defaults to True since bot commands are explicit user actions.
        """
        import sys
        ae_flag = "True" if auto_execute else "False"
        try:
            env = _clean_claude_env()
            result = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    [sys.executable, "-c",
                     f"from pipeline import run_pipeline; run_pipeline(auto_execute={ae_flag})"],
                    capture_output=True,
                    text=True,
                    timeout=600,
                    cwd=str(Path(__file__).parent.parent),
                    env=env,
                ),
            )

            # Forward subprocess stderr (pipeline logs) to bot logger
            if result.stderr:
                for line in result.stderr.strip().splitlines():
                    logger.info("[pipeline] %s", line.strip())

            if result.returncode == 0:
                # Parse stderr for summary info (logging goes to stderr)
                all_lines = (result.stderr or "").strip().splitlines()
                topic_line = next(
                    (l for l in all_lines if "topics found" in l or "dev_topics_found" in l.lower()),
                    None,
                )
                plan_lines = [l for l in all_lines if "plan generated" in l or "Skipping topic" in l]

                summary_parts = []
                if topic_line:
                    clean = topic_line.strip().split("] ")[-1] if "] " in topic_line else topic_line.strip()
                    summary_parts.append(clean)
                for pl in plan_lines[:5]:
                    clean = pl.strip().split("] ")[-1] if "] " in pl else pl.strip()
                    summary_parts.append(f"  {clean}")

                return True, "\n".join(summary_parts)
            else:
                error_excerpt = result.stderr[-1500:] if result.stderr else "Unknown error"
                return False, f"Pipeline error (code {result.returncode}):\n```\n{error_excerpt}\n```"
        except subprocess.TimeoutExpired:
            return False, "Pipeline timed out (10 minutes)."
        except Exception as exc:
            logger.exception("Pipeline execution failed")
            return False, f"Execution failed: {exc}"

    _VALID_PIPELINE_MODES = ("full", "plan")

    def _resolve_pipeline_mode(self, override: str = "") -> str:
        """Resolve pipeline execution mode from override or config.

        Args:
            override: Explicit mode from command argument ("plan" or "full").
                Empty string means use config default.

        Returns:
            "plan" or "full".
        """
        if override in self._VALID_PIPELINE_MODES:
            return override
        return self.config.pipeline_mode if self.config.pipeline_mode in self._VALID_PIPELINE_MODES else "full"

    @commands.command(name="pipeline")
    async def pipeline_command(self, ctx: commands.Context, *, mode: str = "") -> None:
        """Run the analysis pipeline on saved transcription files.

        Usage:
            !pipeline          — run with config default mode
            !pipeline plan     — plan generation only (skip execution)
            !pipeline full     — plan generation + Claude Code execution
            !pipeline help     — show usage
        """
        mode = mode.strip().lower()

        if mode == "help":
            config_mode = self.config.pipeline_mode
            await ctx.send(
                "**Pipeline usage:**\n"
                "`!pipeline` — run with default mode\n"
                "`!pipeline plan` — analysis + plan generation only\n"
                "`!pipeline full` — analysis + plan generation + Claude Code execution\n"
                "`!pipeline help` — show this help\n\n"
                f"Current default mode: **{config_mode}** "
                f"(`config.yaml` → `collector.pipeline_mode`)"
            )
            return

        resolved_mode = self._resolve_pipeline_mode(mode)
        auto_execute = resolved_mode == "full"

        logger.info(
            "!pipeline command received from %s in #%s (%s) [mode=%s, auto_execute=%s]",
            ctx.author, ctx.channel, ctx.guild, resolved_mode, auto_execute,
        )

        mode_label = "full (plan + execute)" if auto_execute else "plan generation only"
        await ctx.send(f"Starting pipeline... (mode: **{mode_label}**)")

        plans_dir = Path(__file__).parent.parent / "data" / "plans"
        existing = set(plans_dir.glob("*.md")) if plans_dir.exists() else set()

        success, message = await self._run_pipeline_subprocess(auto_execute=auto_execute)
        logger.info("Pipeline finished: success=%s, mode=%s", success, resolved_mode)
        if message:
            await ctx.send(message)

        if success:
            new_plans = sorted((set(plans_dir.glob("*.md")) if plans_dir.exists() else set()) - existing)
            logger.info("New plans detected: %d", len(new_plans))
            await self._post_plans_to_channel(ctx.channel, new_plans)

            if auto_execute:
                await ctx.send("✅ Pipeline complete! (plan generation + execution)")
            elif new_plans:
                await ctx.send(
                    "✅ Plan generation complete!\n"
                    "Use `!pipeline full` to execute the plans."
                )
            else:
                await ctx.send("✅ Pipeline complete! (no new plans)")
        else:
            await ctx.send("❌ Pipeline failed.")

    # ── Plan posting ────────────────────────────────────────────────

    async def _post_plans_to_channel(
        self,
        channel: discord.TextChannel,
        plan_files: list[Path],
    ) -> None:
        """Post generated plan summaries to the Discord channel."""
        if not plan_files:
            return

        await channel.send(f"📋 **{len(plan_files)} plan(s) generated:**")

        for plan_file in plan_files:
            try:
                content = plan_file.read_text(encoding="utf-8")
            except OSError:
                continue

            # Extract title
            title_match = re.search(r"^# Plan:\s*(.+)$", content, re.MULTILINE)
            title = title_match.group(1).strip() if title_match else plan_file.stem

            # Extract objective
            obj_match = re.search(
                r"## Objective\n(.+?)(?=\n##|\Z)", content, re.DOTALL
            )
            objective = obj_match.group(1).strip()[:300] if obj_match else ""

            msg = f"**• {title}**"
            if objective:
                msg += f"\n> {objective}"
            msg += f"\n📁 `{plan_file.name}`"

            # Discord message limit is 2000 chars
            if len(msg) > 1900:
                msg = msg[:1900] + "..."

            await channel.send(msg)

    # ── Internal callbacks ────────────────────────────────────────────────────

    async def _recording_finished_callback(
        self,
        sink: discord.sinks.WaveSink,
        text_channel: discord.TextChannel,
        session: RecordingSession,
        *args,
    ) -> None:
        """Transcribes audio with Whisper after recording finishes and saves to file."""
        guild_id = session.guild_id
        self._sessions.pop(guild_id, None)

        # Disconnect from voice channel
        if session.voice_client.is_connected():
            await session.voice_client.disconnect()

        session_end = datetime.now(timezone.utc).isoformat()

        if not sink.audio_data:
            await text_channel.send("No audio was recorded.")
            return

        await text_channel.send(
            f"Transcribing audio from {len(sink.audio_data)} user(s)... (Whisper processing)"
        )

        messages: list[dict[str, Any]] = []
        loop = asyncio.get_running_loop()

        for user_id, audio_data in sink.audio_data.items():
            try:
                user = await self.bot.fetch_user(int(user_id))
                username = user.name
            except Exception:
                username = f"user_{user_id}"

            # BytesIO → bytes
            audio_data.file.seek(0)
            raw_bytes = audio_data.file.read()

            if len(raw_bytes) < 1000:
                # Audio too short (silence) — skip
                logger.debug("User %s: audio too short, skipping (%d bytes)", username, len(raw_bytes))
                continue

            # Run blocking work in thread pool
            text = await loop.run_in_executor(
                None, _transcribe_audio, raw_bytes, self.config.whisper_language, self.config
            )

            if not text.strip():
                logger.debug("User %s: no transcription result", username)
                continue

            messages.append(
                {
                    "id": f"{user_id}_{int(datetime.now(timezone.utc).timestamp())}",
                    "timestamp": session_end,
                    "author": username,
                    "author_id": str(user_id),
                    "content": text,
                    "attachments": [],
                    "edited": False,
                    "type": "voice",
                }
            )
            logger.info("[%s] %s: %s", session.channel_name, username, text[:80])

        if not messages:
            await text_channel.send("No transcription results (silence or unrecognizable audio).")
            return

        # Save to file
        saved_path = await loop.run_in_executor(
            None,
            _save_transcription,
            self.config.data_dir,
            session.guild_name,
            session.channel_name,
            session.started_at,
            session_end,
            messages,
        )

        summary = "\n".join(
            f"**{m['author']}**: {m['content'][:100]}" for m in messages[:5]
        )
        if len(messages) > 5:
            summary += f"\n... and {len(messages) - 5} more utterance(s)"

        await text_channel.send(
            f"Transcription complete ({len(messages)} utterance(s)):\n{summary}\n\n"
            f"Saved to: `{saved_path}`"
        )

        if self.config.auto_pipeline:
            resolved_mode = self._resolve_pipeline_mode()
            auto_execute = resolved_mode == "full"
            mode_label = "full (plan + execute)" if auto_execute else "plan generation only"
            await text_channel.send(f"Running pipeline automatically... (mode: **{mode_label}**)")

            plans_dir = Path(__file__).parent.parent / "data" / "plans"
            existing = set(plans_dir.glob("*.md")) if plans_dir.exists() else set()

            success, message = await self._run_pipeline_subprocess(auto_execute=auto_execute)
            if message:
                await text_channel.send(message)

            if success:
                new_plans = sorted(
                    (set(plans_dir.glob("*.md")) if plans_dir.exists() else set()) - existing
                )
                await self._post_plans_to_channel(text_channel, new_plans)
                if auto_execute:
                    await text_channel.send("✅ Pipeline complete! (plan generation + execution)")
                elif new_plans:
                    await text_channel.send(
                        "✅ Plan generation complete!\n"
                        "Use `!pipeline full` to execute the plans."
                    )
                else:
                    await text_channel.send("✅ Pipeline complete! (no new plans)")
            else:
                await text_channel.send("❌ Pipeline failed.")
        else:
            await text_channel.send(
                "Use `!pipeline` to analyze/generate development plans."
            )


class VoiceBot(commands.Bot):
    """Minimal Bot class — responsible only for registering Cogs."""

    def __init__(self, config: CollectorConfig) -> None:
        intents = discord.Intents.all()
        super().__init__(command_prefix="!", intents=intents)
        self.add_cog(VoiceCog(self, config))


def run() -> None:
    """Bot entry point."""
    config = load_config()
    bot = VoiceBot(config)
    logger.info("Starting voice recording bot...")
    bot.run(config.token)


if __name__ == "__main__":
    run()
