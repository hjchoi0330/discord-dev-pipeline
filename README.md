# Discord Dev Pipeline

Discord 음성 채널의 대화를 실시간으로 녹음/전사하고, 개발 관련 토픽을 자동 추출하여 구현 계획을 생성하고, Claude Code CLI로 자동 개발 요청까지 수행하는 파이프라인.

## Architecture

```
Discord Voice Channel
    │  !join / !leave
    v
┌─────────────────────────┐
│  Collector (bot.py)     │  per-speaker 오디오 캡처 + faster-whisper STT
│  → data/conversations/  │  YYYY-MM-DD_guild_channel_voice.json
└──────────┬──────────────┘
           v
┌─────────────────────────┐
│  Analyzer (analyzer.py) │  claude --print 으로 개발 토픽 추출
│  → data/analysis/       │  YYYY-MM-DD_analysis.json
└──────────┬──────────────┘
           v
┌─────────────────────────┐
│  Planner (planner.py)   │  claude --print 으로 구현 계획 생성
│  → data/plans/          │  YYYY-MM-DD_topic-slug.md
└──────────┬──────────────┘
           v
┌─────────────────────────┐
│  Executor (executor.py) │  claude --print 으로 코드 생성 실행
│  → data/result/         │  프로젝트별 디렉토리
└─────────────────────────┘
```

> 모든 Claude 호출은 `claude --print` (Claude Code CLI) 서브프로세스로 실행됩니다.
> Anthropic API 키 없이, Claude Code에 로그인만 되어 있으면 동작합니다.

## Prerequisites

- Python 3.10+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude` on PATH, 로그인 필요)
- Discord Bot Token
- ffmpeg (`brew install ffmpeg` / `apt install ffmpeg`)
- libopus (`brew install opus` / `apt install libopus0`)

## Installation

```bash
git clone git@github.com:hjchoi0330/discord-dev-pipeline.git
cd discord-dev-pipeline

python -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt

echo 'DISCORD_BOT_TOKEN=your_token_here' > .env

claude --version  # Claude Code CLI 설치 확인
```

## Discord Bot Setup

1. [Discord Developer Portal](https://discord.com/developers/applications) → New Application
2. Bot → Reset Token → `.env`의 `DISCORD_BOT_TOKEN`에 설정
3. Privileged Gateway Intents 3개 모두 활성화 (Presence, Server Members, Message Content)
4. OAuth2 → URL Generator:
   - Scopes: `bot`
   - Permissions: `Connect`, `Speak`, `Use Voice Activity`, `Send Messages`, `Read Message History`
5. 생성된 URL로 봇을 서버에 초대

## Usage

### Bot 시작

```bash
python pipeline.py --bot
```

### Discord Commands

| Command | Description |
|---------|-------------|
| `!join [channel]` | 음성 채널 입장 + 녹음 시작 (채널명 생략 시 현재 채널) |
| `!leave` | 녹음 중지 → Whisper 전사 → 파일 저장 |
| `!status` | 녹음 경과 시간 표시 |
| `!pipeline` | 파이프라인 실행 (config 기본 모드) |
| `!pipeline plan` | 분석 + 계획 생성만 (실행 스킵) |
| `!pipeline full` | 분석 + 계획 생성 + Claude Code 실행 |
| `!topic <description>` | 수동으로 개발 토픽 주입 |
| `!conversations [date]` | 저장된 대화 파일 조회 |
| `!runs [date]` | 파이프라인 실행 이력 조회 |

### CLI

```bash
# 오늘 날짜로 파이프라인 실행
python pipeline.py --run

# 특정 날짜
python pipeline.py --date 2026-04-13

# 재분석 강제
python pipeline.py --date 2026-04-13 --force

# 분석만 (계획 생성 없음)
python pipeline.py --analyze-only

# 드라이 런
python pipeline.py --dry-run --run

# 실행 이력 확인
python pipeline.py --history

# 인터랙티브 메뉴
python pipeline.py
```

### Demo (Claude CLI 없이 테스트)

```bash
python run_demo.py            # 기본 날짜 2026-03-01
python run_demo.py 2026-02-28 # 특정 날짜
```

## Configuration

`config.yaml`:

```yaml
discord:
  token: ""                       # DISCORD_BOT_TOKEN 환경변수 권장

collector:
  data_dir: "data/conversations"
  whisper_model: "medium"         # tiny | base | small | medium | large-v3
  whisper_language: "ko"          # ko, en, ja, zh ...
  whisper_device: "cpu"           # cpu | cuda
  whisper_compute_type: "int8"    # int8 | float16 | float32
  auto_pipeline: false            # true: !leave 후 자동 파이프라인 실행
  pipeline_mode: "plan"           # plan | full

pipeline:
  auto_execute: false             # true: 계획 생성 후 자동 실행

planner:
  timeout_seconds: 600

executor:
  claude_cli_path: "claude"
  timeout_seconds: 300
```

### Whisper 모델 선택

| Model | Size | Speed | Accuracy | Notes |
|-------|------|-------|----------|-------|
| tiny | 75MB | 매우 빠름 | 낮음 | 테스트용 |
| base | 145MB | 빠름 | 보통 | CPU, 짧은 회의 |
| small | 465MB | 보통 | 좋음 | 가성비 추천 |
| **medium** | **1.5GB** | **느림** | **높음** | **현재 설정** |
| large-v3 | 3GB | 매우 느림 | 최고 | GPU 필요 |

## Project Structure

```
discord-dev-pipeline/
├── collector/
│   ├── bot.py              # 음성 녹음 + Whisper 전사 + Discord 피드백
│   ├── config.py           # 봇/Whisper 설정 로딩
│   ├── management.py       # !topic, !conversations, !runs 커맨드
│   └── dave_patch.py       # DAVE(E2E 음성 암호화) 프로토콜 패치
├── analyzer/
│   └── analyzer.py         # 대화에서 개발 토픽 추출 (claude --print)
├── planner/
│   └── planner.py          # 토픽별 구현 계획 Markdown 생성 (claude --print)
├── executor/
│   └── executor.py         # Claude Code로 구현 요청 실행 (claude --print)
├── shared/
│   ├── claude_cli.py       # Claude CLI 호출 래퍼 (지수 백오프 재시도)
│   ├── config.py           # 공용 설정 헬퍼
│   ├── embeds.py           # Discord 임베드 빌더
│   ├── pipeline_state.py   # 파이프라인 실행 상태 추적
│   └── plan_format.py      # 계획 파일 포맷 유틸
├── tests/                  # 219 tests
├── pipeline.py             # 메인 오케스트레이터
├── run_demo.py             # 데모 (mock Claude CLI)
├── config.yaml
└── requirements.txt
```

## Tests

```bash
python -m pytest           # 전체 219개
python -m pytest -v        # verbose
python -m pytest tests/test_analyzer.py  # 특정 모듈
```

모든 테스트는 Claude CLI를 mock하므로 설치 없이 실행 가능.

## Troubleshooting

| Problem | Solution |
|---------|----------|
| 봇이 음성 채널에 들어가지 못함 | Developer Portal에서 Connect/Speak 권한 확인, Intents 3개 활성화 |
| 전사 결과가 비어 있음 | `PyNaCl`, `ffmpeg` 설치 확인, `whisper_language` 설정 확인 |
| Whisper 모델 다운로드 느림 | 최초 1회만 다운로드 (`~/.cache/huggingface/`), GPU 사용 시 `cuda` 설정 |
| `claude` command not found | [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) 설치, `claude --version` 확인 |
| 이미 분석된 파일이 재분석됨 | `--force` 없이는 자동 스킵, 강제 재분석: `--force` 플래그 사용 |
