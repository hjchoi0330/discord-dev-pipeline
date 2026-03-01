# Discord Dev Pipeline

A system that records and transcribes Discord **voice channel** conversations in real time, automatically detects development-related discussions, generates implementation plans, and submits automated development requests to Claude Code CLI.

## System Architecture

```
Discord Voice Channel
    |   !join  -> Bot joins, recording starts
    |   !leave -> Recording stops, transcription begins
    v
[collector/bot.py]          <- Captures per-speaker audio via discord.py sinks
    |   faster-whisper (local open-source STT)
    v  data/conversations/YYYY-MM-DD_guild_channel_voice.json
    |
[analyzer/analyzer.py]      <- Extracts dev topics via claude --print
    |
    v  data/analysis/YYYY-MM-DD_analysis.json
    |
[planner/planner.py]        <- Generates implementation plans via claude --print
    |
    v  data/plans/YYYY-MM-DD_topic-title.md
    |
[executor/executor.py]      <- Submits implementation requests to Claude Code via claude --print
    |
    v  data/result/<project-dir>/  (generated code)
       data/executions/YYYY-MM-DD_execution_log.json
```

> All Claude invocations run `claude --print` (Claude Code CLI) as a subprocess.
> No Anthropic API key is required — you only need to be logged in to Claude Code.

## Prerequisites

- Python 3.11+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude` command must be on PATH, logged in)
- Discord bot token
- ffmpeg (used internally by `discord.py[voice]`, install with `brew install ffmpeg`)
- ~460 MB disk space (Whisper `small` model is downloaded automatically on first run)

## Installation

```bash
# Clone the repository
git clone <repo-url>
cd discord-dev-pipeline

# Create a virtual environment (recommended)
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# Install dependencies (includes discord.py[voice] + faster-whisper)
pip install -r requirements.txt

# Set environment variables
echo 'DISCORD_BOT_TOKEN=your_bot_token_here' > .env

# Verify Claude Code CLI installation
claude --version
```

## Discord Bot Setup

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
2. Click "New Application" and enter a name
3. Navigate to "Bot" in the left menu -> "Reset Token" -> copy the token -> paste it into `DISCORD_BOT_TOKEN` in `.env`
4. Enable all 3 **Privileged Gateway Intents**:
   - Presence Intent
   - Server Members Intent
   - Message Content Intent
5. Navigate to "OAuth2" -> "URL Generator" in the left menu
   - Scopes: `bot`
   - Bot Permissions:
     - `Connect` (join voice channels)
     - `Speak` (speak in voice channels)
     - `Use Voice Activity`
     - `Send Messages` (send results to text channels)
     - `Read Message History`
6. Use the generated URL to invite the bot to your server

## Usage

### Quick Start

```bash
# Start the bot
python pipeline.py --bot
```

Enter commands in a Discord text channel:

```
!join            -> Bot joins your current voice channel and starts recording
!join dev-channel -> Bot joins a specific voice channel by name
!leave           -> Stops recording + auto-transcribes + saves file (triggers auto pipeline if enabled)
!status          -> Shows elapsed recording time
!pipeline        -> Runs the analysis pipeline on saved transcription files (results posted to channel)
```

### Full Workflow

```bash
# 1) Start the bot (separate terminal)
python pipeline.py --bot

# 2) Have a voice conversation in Discord, then save the recording
#    !join -> talk -> !leave

# 3) Run the pipeline (or use !pipeline in Discord)
python pipeline.py --run

# 4) Run for a specific date
python pipeline.py --date 2026-03-01

# 5) Force re-analysis of an already-analyzed date
python pipeline.py --date 2026-03-01 --force

# 6) Run analysis only (no plan generation)
python pipeline.py --analyze-only

# 7) Dry run (pipeline runs without executing Claude Code)
python pipeline.py --dry-run --run
```

### Interactive Menu

```bash
python pipeline.py   # Run without arguments to launch the interactive menu
```

### Demo Mode (Test Without Claude CLI)

Simulates the full pipeline flow without requiring Claude Code CLI:

```bash
# Run the multiplication table demo (default date: 2026-03-01)
python run_demo.py

# Run demo for a specific date
python run_demo.py 2026-02-28
```

`run_demo.py` replaces `_call_claude` with a mock, running the full flow from analysis -> plan generation -> (for multiplication table scenarios) Go file generation.

## How the Pipeline Works

### 1. Voice Recording and Transcription (`collector/bot.py`)

- Bot joins the voice channel when `!join` is issued
- Captures **per-speaker** audio streams separately using `discord.sinks.WaveSink()`
- On `!leave`, recording stops -> local transcription via [faster-whisper](https://github.com/SYSTRAN/faster-whisper)
- Transcription is saved to `data/conversations/YYYY-MM-DD_servername_channelname_voice.json`
- Each utterance is automatically tagged with the speaker's name in the `author` field
- When `auto_pipeline: true` is set, the pipeline runs automatically after `!leave` and posts results to the Discord channel

### 2. Development Topic Analysis (`analyzer/analyzer.py`)

- Analyzes saved transcription files using `claude --print` (via a Claude Code CLI session)
- Automatically detects feature requests, bug reports, architecture discussions, and more
- Evaluates actionability, priority (high/medium/low), and complexity
- Date boundary handling: also searches the previous day's files to catch conversations that started late at night
- Deduplication: files already analyzed are automatically skipped via the `source_files` field (`--force` to re-analyze)
- Fault tolerance: if a conversation file is corrupted or a Claude CLI call fails, that item is skipped and analysis continues

### 3. Plan Generation (`planner/planner.py`)

- Generates a detailed implementation plan Markdown file for each development topic
- Includes a ready-to-use prompt in the `## Claude Code Prompt` section
- Plans are sorted by priority (high -> medium -> low)
- If a Claude CLI call fails, that topic is skipped and plan generation continues for the rest

### 4. Claude Code Execution (`executor/executor.py`)

- Extracts the prompt from the `## Claude Code Prompt` section of each plan file
- Submits the implementation request to Claude Code via `claude --print --dangerously-skip-permissions`
- Output is saved to `data/result/<project-directory>/` (project directory name is extracted from the plan)
- If the Claude CLI is unavailable, prompts are saved to `data/pending_executions/` for manual execution
- Execution logs are recorded in `data/executions/YYYY-MM-DD_execution_log.json`

> When `pipeline.auto_execute: true` is set in `config.yaml`, Claude Code is executed automatically after plan generation.
> The default is `false`, in which case the pipeline stops after generating plan files.

## Configuration (`config.yaml`)

```yaml
discord:
  token: ""                      # Prefer setting via DISCORD_BOT_TOKEN environment variable

collector:
  data_dir: "data/conversations" # Transcription file storage path (overridable via DATA_DIR env var)
  whisper_model: "small"         # tiny | base | small | medium | large-v3
  whisper_language: "ko"         # Transcription language (ko, en, ja, zh ...)
  whisper_device: "cpu"          # cpu | cuda (GPU acceleration)
  whisper_compute_type: "int8"   # int8 | float16 | float32
  auto_pipeline: false           # true: auto-run pipeline after !leave + post results to Discord

  monitored_voice_channels: []   # Empty list = monitor all voice channels
  ignored_voice_channels:
    - "AFK"

pipeline:
  data_dir: "data"               # Root data directory
  auto_execute: false            # true: auto-execute Claude Code after plan generation

analyzer:
  model: "claude-sonnet-4-6"     # Model to use for analysis (invoked via CLI session)
  min_messages_for_topic: 3      # Minimum number of messages required to qualify as a topic

planner:
  model: "claude-sonnet-4-6"

executor:
  claude_cli_path: "claude"      # Path to the claude CLI binary
  timeout_seconds: 300           # Claude Code execution timeout (seconds)
```

### Whisper Model Selection Guide

| Model | Size | Speed | Accuracy | Recommended For |
|-------|------|-------|----------|-----------------|
| tiny | 75MB | Very fast | Low | Quick testing |
| base | 145MB | Fast | Moderate | CPU, short meetings |
| **small** | **465MB** | **Moderate** | **Good** | **Default (recommended)** |
| medium | 1.5GB | Slow | High | GPU recommended |
| large-v3 | 3GB | Very slow | Best | GPU required |

## Directory Structure

```
discord-dev-pipeline/
├── collector/                    # Discord voice recording bot
│   ├── bot.py                    # Voice recording + Whisper transcription + Discord feedback
│   └── config.py                 # Bot and Whisper configuration
├── analyzer/                     # Transcription text analysis
│   └── analyzer.py               # Extracts dev topics via claude --print
├── planner/                      # Development plan generation
│   └── planner.py                # Generates Markdown plans via claude --print
├── executor/                     # Claude Code execution
│   └── executor.py               # Submits implementation requests via claude --print
├── shared/                       # Shared modules
│   └── claude_cli.py             # Claude Code CLI invocation wrapper
├── data/
│   ├── conversations/            # Transcribed voice conversations (JSON)
│   ├── analysis/                 # Analysis results
│   ├── plans/                    # Generated development plans (Markdown)
│   ├── result/                   # Execution output (per-project directories)
│   ├── executions/               # Execution logs
│   └── pending_executions/       # Prompt staging area when claude CLI is unavailable
├── tests/
│   ├── test_analyzer.py          # Analyzer unit tests + error handling
│   ├── test_planner.py           # Planner unit tests + CLI failure handling
│   ├── test_executor.py          # Executor unit tests
│   ├── test_pipeline.py          # Pipeline integration tests
│   ├── test_shared_claude_cli.py # Claude CLI wrapper tests
│   └── test_e2e_gugudan.py       # E2E tests (multiplication table scenario)
├── pipeline.py                   # Main orchestrator
├── run_demo.py                   # Demo runner (simulates pipeline without Claude CLI)
├── config.yaml                   # Configuration file
├── .env                          # Environment variables (excluded from git)
└── requirements.txt
```

## Tests

```bash
# Run all tests (96 total)
python -m pytest

# Run a specific module
python -m pytest tests/test_analyzer.py
python -m pytest tests/test_e2e_gugudan.py

# Verbose output
python -m pytest -v
```

All tests mock Claude CLI calls, so no Claude Code installation is required to run them.

## Troubleshooting

**Bot cannot join voice channel**
- Verify `Connect` and `Speak` permissions in the Discord Developer Portal
- Regenerate the bot invite URL and re-invite the bot
- Confirm all 3 Privileged Gateway Intents are enabled

**Transcription is missing or empty**
- Check `PyNaCl` installation: `pip install PyNaCl`
- Check `ffmpeg` installation: `brew install ffmpeg` (macOS) / `apt install ffmpeg` (Ubuntu)
- Confirm there was actual speech in the voice channel (silence is skipped)
- Verify `whisper_language` in `config.yaml`

**Whisper model download is slow**
- The model is only downloaded once and cached at `~/.cache/huggingface/`
- For GPU acceleration: set `whisper_device: "cuda"` and `whisper_compute_type: "float16"`
- For quick testing: set `whisper_model: "tiny"`

**`claude` command not found**
- Install Claude Code CLI: https://docs.anthropic.com/en/docs/claude-code
- Verify installation with `claude --version`
- You can specify the full path in `config.yaml` under `executor.claude_cli_path`
- To test without the CLI: `python run_demo.py`

**Already-analyzed files are being re-analyzed**
- Without the `--force` flag, previously analyzed files are automatically skipped
- Tracking is done via the `source_files` field in `data/analysis/YYYY-MM-DD_analysis.json`
- To force re-analysis: `python pipeline.py --date YYYY-MM-DD --force`
