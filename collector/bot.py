"""Discord 음성 채널 녹음 봇 - 음성 대화를 Whisper로 전사하여 파일로 저장합니다."""

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
from io import BytesIO
from pathlib import Path
from typing import Any

import discord
from discord.ext import commands

# macOS Homebrew의 libopus 로드 (음성 디코딩에 필수)
if not discord.opus.is_loaded():
    try:
        discord.opus.load_opus("/opt/homebrew/lib/libopus.dylib")
    except OSError:
        pass  # 다른 OS에서는 시스템 기본 경로에서 자동 로드

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
    """faster-whisper를 사용해 오디오 바이트를 텍스트로 전사합니다."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        logger.error("faster-whisper가 설치되지 않았습니다. pip install faster-whisper 실행 후 재시도하세요.")
        return ""

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    try:
        # 모델은 프로세스 생애 동안 캐시됨 (첫 호출 시 다운로드)
        model = _get_whisper_model(config)
        segments, info = model.transcribe(
            tmp_path,
            language=language,
            beam_size=5,
            vad_filter=True,  # 무음 구간 자동 제거
            vad_parameters={"min_silence_duration_ms": 500},
        )
        text = " ".join(seg.text.strip() for seg in segments if seg.text.strip())
        logger.debug("전사 결과 (%.1fs): %s", info.duration, text[:100])
        return text
    finally:
        Path(tmp_path).unlink(missing_ok=True)


_whisper_model = None
_whisper_lock = threading.Lock()


def _get_whisper_model(config: CollectorConfig | None = None):
    """Whisper 모델 싱글톤 (thread-safe)."""
    global _whisper_model
    if _whisper_model is None:
        with _whisper_lock:
            if _whisper_model is None:
                from faster_whisper import WhisperModel
                model_size = config.whisper_model if config else "small"
                device = config.whisper_device if config else "cpu"
                compute_type = config.whisper_compute_type if config else "int8"
                logger.info("Whisper 모델 로딩 중 (%s, %s)...", model_size, device)
                _whisper_model = WhisperModel(
                    model_size,
                    device=device,
                    compute_type=compute_type,
                )
                logger.info("Whisper 모델 준비 완료")
    return _whisper_model


def _save_transcription(
    data_dir: str,
    guild_name: str,
    channel_name: str,
    session_start: str,
    session_end: str,
    messages: list[dict[str, Any]],
) -> Path:
    """전사 결과를 JSON 파일로 저장합니다."""
    date_str = session_start[:10]
    safe_guild = "".join(c if c.isalnum() or c in "-_" else "_" for c in guild_name)
    safe_channel = "".join(c if c.isalnum() or c in "-_" else "_" for c in channel_name)
    filename = f"{date_str}_{safe_guild}_{safe_channel}_voice.json"
    path = Path(data_dir) / filename
    path.parent.mkdir(parents=True, exist_ok=True)

    lock = _get_file_lock(str(path))
    with lock:
        # 기존 파일이 있으면 세션 추가 (같은 날 여러 세션 지원)
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

    logger.info("전사 결과 저장: %s (%d개 발화)", path, len(messages))
    return path


class VoiceCog(commands.Cog):
    """음성 녹음 및 파이프라인 명령어 Cog."""

    def __init__(self, bot: commands.Bot, config: CollectorConfig) -> None:
        self.bot = bot
        self.config = config
        # guild_id → RecordingSession
        self._sessions: dict[int, RecordingSession] = {}

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        logger.info("봇 로그인 완료: %s (id=%s)", self.bot.user, self.bot.user.id)  # type: ignore[union-attr]
        logger.info("음성 채널 녹음 모드 활성화. '!join [채널명]'으로 음성 채널에 접속하세요.")

    # ── 음성 채널 명령어 ─────────────────────────────────────────────

    @commands.command(name="join")
    async def join_command(self, ctx: commands.Context, *, channel_name: str = "") -> None:
        """음성 채널에 접속하여 녹음을 시작합니다.

        Usage: !join [채널명]  (채널명 생략 시 사용자의 현재 채널 입장)
        """
        if not isinstance(ctx.guild, discord.Guild):
            await ctx.send("서버 내에서 사용해야 합니다.")
            return

        # 채널 찾기: 명시적 이름 > 사용자 현재 채널
        target_channel: discord.VoiceChannel | None = None
        if channel_name:
            target_channel = discord.utils.get(ctx.guild.voice_channels, name=channel_name)
            if not target_channel:
                await ctx.send(f"음성 채널 '{channel_name}'을 찾을 수 없습니다.")
                return
        elif isinstance(ctx.author, discord.Member) and ctx.author.voice:
            target_channel = ctx.author.voice.channel  # type: ignore[assignment]
        else:
            await ctx.send("채널명을 지정하거나 음성 채널에 먼저 입장하세요.")
            return

        guild_id = ctx.guild.id
        if guild_id in self._sessions:
            await ctx.send("이미 녹음 중입니다. '!leave'로 먼저 종료하세요.")
            return

        try:
            vc = await target_channel.connect()
        except discord.ClientException as e:
            await ctx.send(f"채널 접속 실패: {e}")
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

        # discord.py sinks로 녹음 시작
        vc.start_recording(
            discord.sinks.WaveSink(),
            self._recording_finished_callback,
            ctx.channel,  # 완료 후 결과 보낼 텍스트 채널
            session,
        )

        await ctx.send(
            f"**#{target_channel.name}** 채널 녹음을 시작합니다. "
            f"종료하려면 `!leave`를 입력하세요."
        )
        logger.info("[%s] 녹음 시작: #%s", ctx.guild.name, target_channel.name)

    @commands.command(name="leave")
    async def leave_command(self, ctx: commands.Context) -> None:
        """녹음을 종료하고 음성 채널에서 나갑니다.

        Usage: !leave
        """
        if not isinstance(ctx.guild, discord.Guild):
            await ctx.send("서버 내에서 사용해야 합니다.")
            return

        guild_id = ctx.guild.id
        if guild_id not in self._sessions:
            await ctx.send("현재 녹음 중인 세션이 없습니다.")
            return

        session = self._sessions[guild_id]
        await ctx.send("녹음을 종료합니다. 전사 처리 중...")

        # stop_recording이 _recording_finished_callback을 트리거함
        session.voice_client.stop_recording()

    @commands.command(name="status")
    async def status_command(self, ctx: commands.Context) -> None:
        """현재 녹음 상태를 확인합니다."""
        if not isinstance(ctx.guild, discord.Guild):
            return
        session = self._sessions.get(ctx.guild.id)
        if session:
            elapsed = datetime.now(timezone.utc).timestamp() - datetime.fromisoformat(
                session.started_at
            ).timestamp()
            await ctx.send(
                f"녹음 중: **#{session.channel_name}** "
                f"(경과 {int(elapsed // 60)}분 {int(elapsed % 60)}초)"
            )
        else:
            await ctx.send("현재 녹음 중인 세션이 없습니다.")

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
                    (l for l in all_lines if "토픽 발견" in l or "dev_topics_found" in l.lower()),
                    None,
                )
                plan_lines = [l for l in all_lines if "계획 생성" in l or "Skipping topic" in l]

                summary_parts = []
                if topic_line:
                    clean = topic_line.strip().split("] ")[-1] if "] " in topic_line else topic_line.strip()
                    summary_parts.append(clean)
                for pl in plan_lines[:5]:
                    clean = pl.strip().split("] ")[-1] if "] " in pl else pl.strip()
                    summary_parts.append(f"  {clean}")

                return True, "\n".join(summary_parts)
            else:
                error_excerpt = result.stderr[-1500:] if result.stderr else "알 수 없는 오류"
                return False, f"파이프라인 오류 (코드 {result.returncode}):\n```\n{error_excerpt}\n```"
        except subprocess.TimeoutExpired:
            return False, "파이프라인 타임아웃 (10분)."
        except Exception as exc:
            logger.exception("파이프라인 실행 실패")
            return False, f"실행 실패: {exc}"

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
            !pipeline          — config 기본 모드로 실행
            !pipeline plan     — 계획 생성까지만 (실행 생략)
            !pipeline full     — 계획 생성 + Claude Code 실행
            !pipeline help     — 사용법 표시
        """
        mode = mode.strip().lower()

        if mode == "help":
            config_mode = self.config.pipeline_mode
            await ctx.send(
                "**파이프라인 사용법:**\n"
                "`!pipeline` — 기본 모드로 실행\n"
                "`!pipeline plan` — 분석 + 계획 생성까지만\n"
                "`!pipeline full` — 분석 + 계획 생성 + Claude Code 실행\n"
                "`!pipeline help` — 이 도움말 표시\n\n"
                f"현재 기본 모드: **{config_mode}** "
                f"(`config.yaml` → `collector.pipeline_mode`)"
            )
            return

        resolved_mode = self._resolve_pipeline_mode(mode)
        auto_execute = resolved_mode == "full"

        logger.info(
            "!pipeline command received from %s in #%s (%s) [mode=%s, auto_execute=%s]",
            ctx.author, ctx.channel, ctx.guild, resolved_mode, auto_execute,
        )

        mode_label = "전체 (계획 + 실행)" if auto_execute else "계획 생성까지만"
        await ctx.send(f"파이프라인 시작 중... (모드: **{mode_label}**)")

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
                await ctx.send("✅ 파이프라인 완료! (계획 생성 + 실행)")
            elif new_plans:
                await ctx.send(
                    "✅ 계획 생성 완료!\n"
                    "`!pipeline full`로 계획을 실행할 수 있습니다."
                )
            else:
                await ctx.send("✅ 파이프라인 완료! (새 계획 없음)")
        else:
            await ctx.send("❌ 파이프라인 실패.")

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

    # ── 내부 콜백 ────────────────────────────────────────────────────

    async def _recording_finished_callback(
        self,
        sink: discord.sinks.WaveSink,
        text_channel: discord.TextChannel,
        session: RecordingSession,
        *args,
    ) -> None:
        """녹음 완료 후 Whisper로 전사하고 파일에 저장합니다."""
        guild_id = session.guild_id
        self._sessions.pop(guild_id, None)

        # 음성 채널 연결 해제
        if session.voice_client.is_connected():
            await session.voice_client.disconnect()

        session_end = datetime.now(timezone.utc).isoformat()

        if not sink.audio_data:
            await text_channel.send("녹음된 오디오가 없습니다.")
            return

        await text_channel.send(
            f"{len(sink.audio_data)}명의 오디오 전사 중... (Whisper 처리 중)"
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
                # 너무 짧은 오디오 (무음) 건너뜀
                logger.debug("사용자 %s: 오디오 너무 짧아 건너뜀 (%d bytes)", username, len(raw_bytes))
                continue

            # 블로킹 작업을 스레드풀에서 실행
            text = await loop.run_in_executor(
                None, _transcribe_audio, raw_bytes, self.config.whisper_language, self.config
            )

            if not text.strip():
                logger.debug("사용자 %s: 전사 결과 없음", username)
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
            await text_channel.send("전사된 내용이 없습니다 (무음 또는 인식 불가).")
            return

        # 파일 저장
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
            summary += f"\n... 외 {len(messages) - 5}개 발화"

        await text_channel.send(
            f"전사 완료 ({len(messages)}개 발화):\n{summary}\n\n"
            f"저장 위치: `{saved_path}`"
        )

        if self.config.auto_pipeline:
            resolved_mode = self._resolve_pipeline_mode()
            auto_execute = resolved_mode == "full"
            mode_label = "전체 (계획 + 실행)" if auto_execute else "계획 생성까지만"
            await text_channel.send(f"파이프라인 자동 실행 중... (모드: **{mode_label}**)")

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
                    await text_channel.send("✅ 파이프라인 완료! (계획 생성 + 실행)")
                elif new_plans:
                    await text_channel.send(
                        "✅ 계획 생성 완료!\n"
                        "`!pipeline full`로 계획을 실행할 수 있습니다."
                    )
                else:
                    await text_channel.send("✅ 파이프라인 완료! (새 계획 없음)")
            else:
                await text_channel.send("❌ 파이프라인 실패.")
        else:
            await text_channel.send(
                f"`!pipeline`으로 개발 계획을 분석/생성할 수 있습니다."
            )


class VoiceBot(commands.Bot):
    """최소 Bot 클래스 — Cog 등록만 담당합니다."""

    def __init__(self, config: CollectorConfig) -> None:
        intents = discord.Intents.all()
        super().__init__(command_prefix="!", intents=intents)
        self.add_cog(VoiceCog(self, config))


def run() -> None:
    """봇 진입점."""
    config = load_config()
    bot = VoiceBot(config)
    logger.info("음성 녹음 봇 시작 중...")
    bot.run(config.token)


if __name__ == "__main__":
    run()
