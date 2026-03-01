# Discord Dev Pipeline

Discord **음성 채널** 대화를 실시간으로 녹음 및 전사하여 개발 관련 논의를 자동으로 감지하고,
구현 계획을 생성한 뒤 Claude Code CLI로 자동 개발을 요청하는 시스템입니다.

## 시스템 아키텍처

```
Discord 음성 채널
    |   !join  -> 봇 입장, 녹음 시작
    |   !leave -> 녹음 종료, 전사 시작
    v
[collector/bot.py]          <- discord.py sinks로 각 화자별 오디오 캡처
    |   faster-whisper (로컬 오픈소스 STT)
    v  data/conversations/YYYY-MM-DD_guild_channel_voice.json
    |
[analyzer/analyzer.py]      <- claude --print 로 개발 토픽 추출
    |
    v  data/analysis/YYYY-MM-DD_analysis.json
    |
[planner/planner.py]        <- claude --print 로 구현 계획 생성
    |
    v  data/plans/YYYY-MM-DD_topic-title.md
    |
[executor/executor.py]      <- claude --print 로 Claude Code에 구현 요청
    |
    v  data/result/<project-dir>/  (생성된 코드)
       data/executions/YYYY-MM-DD_execution_log.json
```

> 모든 Claude 호출은 `claude --print` (Claude Code CLI)를 subprocess로 실행합니다.
> Anthropic API 키는 필요 없으며, Claude Code에 로그인된 상태이면 됩니다.

## 사전 요구사항

- Python 3.11+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude` 명령어가 PATH에 있어야 함, 로그인 완료 상태)
- Discord 봇 토큰
- ffmpeg (`discord.py[voice]`가 내부적으로 사용, `brew install ffmpeg`)
- 약 460MB 디스크 (Whisper `small` 모델 최초 자동 다운로드)

## 설치

```bash
# 저장소 클론
git clone <repo-url>
cd discord-dev-pipeline

# 가상환경 생성 (권장)
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# 의존성 설치 (discord.py[voice] + faster-whisper 포함)
pip install -r requirements.txt

# 환경변수 설정
echo 'DISCORD_BOT_TOKEN=여기에_봇_토큰' > .env

# Claude Code CLI 로그인 확인
claude --version
```

## Discord 봇 설정

1. [Discord Developer Portal](https://discord.com/developers/applications) 접속
2. "New Application" 클릭 -> 이름 입력
3. 좌측 메뉴 "Bot" -> "Reset Token" -> 토큰 복사 -> `.env`의 `DISCORD_BOT_TOKEN`에 입력
4. **Privileged Gateway Intents** 3개 활성화:
   - Presence Intent
   - Server Members Intent
   - Message Content Intent
5. 좌측 메뉴 "OAuth2" -> "URL Generator"
   - Scopes: `bot`
   - Bot Permissions:
     - `Connect` (음성 채널 입장)
     - `Speak` (음성 채널 발화)
     - `Use Voice Activity`
     - `Send Messages` (텍스트 채널 결과 전송)
     - `Read Message History`
6. 생성된 URL로 봇을 서버에 초대

## 사용법

### 빠른 시작

```bash
# 봇 시작
python pipeline.py --bot
```

Discord 텍스트 채널에서 명령어 입력:

```
!join            -> 현재 입장한 음성 채널에 봇 참가 + 녹음 시작
!join 개발채널    -> 특정 음성 채널 이름으로 입장
!leave           -> 녹음 종료 + 자동 전사 + 파일 저장 (auto_pipeline 시 자동 분석)
!status          -> 현재 녹음 경과 시간 확인
!pipeline        -> 저장된 전사 파일로 분석 파이프라인 실행 (결과를 채널에 피드백)
```

### 전체 워크플로우

```bash
# 1) 봇 시작 (별도 터미널)
python pipeline.py --bot

# 2) Discord에서 음성 대화 후 녹음 저장
#    !join -> 대화 -> !leave

# 3) 파이프라인 실행 (또는 Discord에서 !pipeline)
python pipeline.py --run

# 4) 특정 날짜로 실행
python pipeline.py --date 2026-03-01

# 5) 이미 분석된 날짜도 강제 재분석
python pipeline.py --date 2026-03-01 --force

# 6) 분석만 실행 (계획 생성 없음)
python pipeline.py --analyze-only

# 7) 시뮬레이션 (Claude Code 실행 안 함)
python pipeline.py --dry-run --run
```

### 대화형 메뉴

```bash
python pipeline.py   # 인수 없이 실행 -> 대화형 메뉴
```

### 데모 모드 (Claude CLI 없이 테스트)

Claude Code CLI 없이 파이프라인 전체 흐름을 시뮬레이션합니다:

```bash
# 구구단 시나리오 데모 (기본: 2026-03-01)
python run_demo.py

# 특정 날짜 데모
python run_demo.py 2026-02-28
```

`run_demo.py`는 `_call_claude`를 mock으로 대체하여 분석 -> 계획 생성 -> (구구단일 경우) Go 파일 생성까지 실행합니다.

## 파이프라인 동작 방식

### 1. 음성 녹음 및 전사 (`collector/bot.py`)

- `!join` 명령 시 봇이 음성 채널에 입장
- `discord.sinks.WaveSink()`로 **화자별** 오디오를 분리 캡처
- `!leave` 시 녹음 종료 -> [faster-whisper](https://github.com/SYSTRAN/faster-whisper)로 로컬 전사
- 전사 결과를 `data/conversations/YYYY-MM-DD_서버명_채널명_voice.json`에 저장
- 각 발화에 `author` 필드로 화자 이름이 자동 기록됨
- `auto_pipeline: true` 설정 시 `!leave` 후 자동으로 파이프라인 실행 및 결과를 Discord 채널에 피드백

### 2. 개발 토픽 분석 (`analyzer/analyzer.py`)

- 저장된 전사 파일을 `claude --print`로 분석 (Claude Code CLI 세션 활용)
- 기능 요청, 버그 리포트, 아키텍처 논의 등을 자동 감지
- 실행 가능성(actionable), 우선순위(high/medium/low), 복잡도 평가
- 날짜 경계 처리: 전날 23시에 시작된 대화도 놓치지 않도록 전날 파일 포함 탐색
- 중복 방지: `source_files` 필드로 이미 분석된 파일은 자동 스킵 (`--force`로 재분석 가능)
- 에러 내성: 손상된 대화 파일이나 Claude CLI 호출 실패 시 해당 항목만 건너뛰고 나머지 분석 계속

### 3. 계획 생성 (`planner/planner.py`)

- 각 개발 토픽별 상세 구현 계획 마크다운 파일 생성
- `## Claude Code Prompt` 섹션에 바로 사용 가능한 프롬프트 포함
- 우선순위 순(high -> medium -> low)으로 정렬
- Claude CLI 호출 실패 시 해당 토픽만 건너뛰고 나머지 계획 생성 계속

### 4. Claude Code 실행 (`executor/executor.py`)

- 각 계획 파일에서 `## Claude Code Prompt` 섹션의 프롬프트를 추출
- `claude --print --dangerously-skip-permissions` 명령으로 Claude Code에 구현 요청
- 개발 결과물은 `data/result/<프로젝트 디렉토리>/`에 저장 (plan 내용에서 프로젝트 디렉토리 자동 추출)
- Claude CLI가 없으면 `data/pending_executions/`에 프롬프트를 저장하여 수동 실행 가능
- 실행 로그는 `data/executions/YYYY-MM-DD_execution_log.json`에 기록

> `config.yaml`의 `pipeline.auto_execute: true` 설정 시 계획 생성 후 자동으로 Claude Code를 실행합니다.
> 기본값은 `false`이며, 이 경우 계획 파일 생성까지만 자동 실행됩니다.

## 설정 (`config.yaml`)

```yaml
discord:
  token: ""                      # 환경변수 DISCORD_BOT_TOKEN 권장

collector:
  data_dir: "data/conversations" # 전사 파일 저장 경로 (DATA_DIR 환경변수로 오버라이드 가능)
  whisper_model: "small"         # tiny | base | small | medium | large-v3
  whisper_language: "ko"         # 전사 언어 (ko, en, ja, zh ...)
  whisper_device: "cpu"          # cpu | cuda (GPU 가속)
  whisper_compute_type: "int8"   # int8 | float16 | float32
  auto_pipeline: false           # true: !leave 후 자동으로 파이프라인 실행 + Discord 피드백

  monitored_voice_channels: []   # 빈 목록 = 모든 음성 채널
  ignored_voice_channels:
    - "AFK"

pipeline:
  data_dir: "data"               # 데이터 루트 디렉토리
  auto_execute: false            # true: 계획 생성 후 자동으로 Claude Code 실행

analyzer:
  model: "claude-sonnet-4-6"     # 분석에 사용할 모델 (CLI 세션 통해 호출)
  min_messages_for_topic: 3      # 토픽으로 인정할 최소 메시지 수

planner:
  model: "claude-sonnet-4-6"

executor:
  claude_cli_path: "claude"      # claude CLI 바이너리 경로
  timeout_seconds: 300           # Claude Code 실행 타임아웃 (초)
```

### Whisper 모델 선택 가이드

| 모델 | 크기 | 속도 | 정확도 | 권장 환경 |
|------|------|------|--------|-----------|
| tiny | 75MB | 매우 빠름 | 낮음 | 빠른 테스트 |
| base | 145MB | 빠름 | 보통 | CPU, 짧은 회의 |
| **small** | **465MB** | **보통** | **좋음** | **기본값 (권장)** |
| medium | 1.5GB | 느림 | 높음 | GPU 권장 |
| large-v3 | 3GB | 매우 느림 | 최고 | GPU 필수 |

## 디렉토리 구조

```
discord-dev-pipeline/
├── collector/                    # Discord 음성 녹음 봇
│   ├── bot.py                    # 음성 녹음 + Whisper 전사 + Discord 피드백
│   └── config.py                 # 봇 및 Whisper 설정
├── analyzer/                     # 전사 텍스트 분석
│   └── analyzer.py               # claude --print 로 개발 토픽 추출
├── planner/                      # 개발 계획 생성
│   └── planner.py                # claude --print 로 마크다운 계획 생성
├── executor/                     # Claude Code 실행
│   └── executor.py               # claude --print 로 구현 요청
├── shared/                       # 공통 모듈
│   └── claude_cli.py             # Claude Code CLI 호출 래퍼
├── data/
│   ├── conversations/            # 전사된 음성 대화 (JSON)
│   ├── analysis/                 # 분석 결과
│   ├── plans/                    # 생성된 개발 계획 (Markdown)
│   ├── result/                   # 실행 결과물 (프로젝트별 디렉토리)
│   ├── executions/               # 실행 로그
│   └── pending_executions/       # claude CLI 없을 때 프롬프트 임시 저장
├── tests/
│   ├── test_analyzer.py          # 분석기 단위 테스트 + 에러 핸들링
│   ├── test_planner.py           # 계획 생성기 단위 테스트 + CLI 실패 처리
│   ├── test_executor.py          # 실행기 단위 테스트
│   ├── test_pipeline.py          # 파이프라인 통합 테스트
│   ├── test_shared_claude_cli.py # Claude CLI 래퍼 테스트
│   └── test_e2e_gugudan.py       # E2E 테스트 (구구단 시나리오)
├── pipeline.py                   # 메인 오케스트레이터
├── run_demo.py                   # 데모 실행기 (Claude CLI 없이 시뮬레이션)
├── config.yaml                   # 설정 파일
├── .env                          # 환경변수 (git 제외)
└── requirements.txt
```

## 테스트

```bash
# 전체 테스트 실행 (96개)
python -m pytest

# 특정 모듈만
python -m pytest tests/test_analyzer.py
python -m pytest tests/test_e2e_gugudan.py

# 상세 출력
python -m pytest -v
```

테스트는 모든 Claude CLI 호출을 mock으로 대체하므로 Claude Code 설치 없이 실행 가능합니다.

## 문제 해결

**봇이 음성 채널에 입장하지 못하는 경우**
- Discord Developer Portal에서 `Connect`, `Speak` 권한 확인
- 봇 초대 URL 재생성 후 재초대
- Privileged Gateway Intents 3개 모두 활성화되었는지 확인

**전사 결과가 없거나 비어있는 경우**
- `PyNaCl` 설치 확인: `pip install PyNaCl`
- `ffmpeg` 설치 확인: `brew install ffmpeg` (macOS) / `apt install ffmpeg` (Ubuntu)
- 음성 채널에 실제 발화가 있었는지 확인 (무음은 건너뜀)
- `config.yaml`의 `whisper_language` 설정 확인

**Whisper 모델 다운로드가 느린 경우**
- 최초 1회만 다운로드됨 (`~/.cache/huggingface/`에 캐시)
- GPU 사용 시: `whisper_device: "cuda"`, `whisper_compute_type: "float16"`
- 빠른 테스트가 목적이면: `whisper_model: "tiny"`

**`claude` 명령을 찾을 수 없는 경우**
- Claude Code CLI 설치: https://docs.anthropic.com/en/docs/claude-code
- `claude --version`으로 설치 확인
- `config.yaml`의 `executor.claude_cli_path`에 전체 경로 지정 가능
- CLI 없이 테스트하려면: `python run_demo.py`

**이미 분석된 파일이 재분석되는 경우**
- `--force` 플래그 없이 실행하면 이전에 분석된 파일은 자동으로 스킵됩니다
- `data/analysis/YYYY-MM-DD_analysis.json`의 `source_files` 필드로 추적
- 강제 재분석이 필요하면: `python pipeline.py --date YYYY-MM-DD --force`
