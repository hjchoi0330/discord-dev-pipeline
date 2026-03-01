#!/usr/bin/env python3
"""파이프라인 데모 실행기 - Claude CLI 호출을 시뮬레이션합니다."""

import json
import re
from pathlib import Path
from unittest.mock import patch

# ── 2026-02-28 시뮬레이션 응답 (기존) ──────────────────────────────────

_MOCK_ANALYSIS_RESPONSE_0228 = json.dumps({
    "dev_topics": [
        {
            "title": "Docker 메모리 설정 및 Health Check 추가",
            "category": "infrastructure",
            "priority": "high",
            "summary": "스테이징 서버에서 Docker 컨테이너가 OOM으로 반복 종료되는 문제가 발생. 메모리 limit을 256MB에서 512MB로 상향하고, liveness/readiness probe를 추가하여 컨테이너 상태를 모니터링해야 함.",
            "keywords": ["docker", "OOM", "health-check", "liveness-probe", "docker-compose", "memory-limit"],
            "actionable": True,
            "estimated_complexity": "small",
            "relevant_message_indices": [0, 1, 2, 10, 11]
        },
        {
            "title": "대시보드 실시간 알림 SSE 구현",
            "category": "feature",
            "priority": "high",
            "summary": "사용자 대시보드에 실시간 알림 기능 요청. SSE(Server-Sent Events) 방식으로 구현하며, Nest.js @Sse 데코레이터와 React EventSource API를 활용. 인증 토큰은 커스텀 EventSource 래퍼로 Authorization 헤더에 포함.",
            "keywords": ["SSE", "real-time", "notification", "NestJS", "React", "EventSource", "WebSocket"],
            "actionable": True,
            "estimated_complexity": "medium",
            "relevant_message_indices": [3, 4, 5, 6, 10, 11]
        },
        {
            "title": "월간 리포트 API 성능 최적화",
            "category": "bug",
            "priority": "medium",
            "summary": "/api/reports/monthly 엔드포인트 응답 시간이 3초 이상 소요. TypeORM의 N+1 쿼리 문제와 캐싱 부재가 원인으로 추정. eager loading 적용과 Redis 캐시(5분 TTL) 도입으로 해결 가능.",
            "keywords": ["performance", "N+1", "TypeORM", "Redis", "caching", "API", "optimization"],
            "actionable": True,
            "estimated_complexity": "medium",
            "relevant_message_indices": [7, 8, 9, 10, 11]
        }
    ]
})

# ── 2026-03-01 시뮬레이션 응답 (Golang 구구단) ─────────────────────────

_MOCK_ANALYSIS_RESPONSE_0301 = json.dumps({
    "dev_topics": [
        {
            "title": "Go 구구단 CLI 프로그램 개발",
            "category": "feature",
            "priority": "medium",
            "summary": "신입 온보딩용 Go 구구단 CLI 프로그램을 개발. fmt.Scanf로 단수를 입력받고 for 루프로 결과를 출력. 입력 검증(숫자 여부, 1~9 범위) 포함.",
            "keywords": ["golang", "CLI", "구구단", "온보딩", "Go"],
            "actionable": True,
            "estimated_complexity": "small",
            "relevant_message_indices": [0, 1, 2, 3, 4]
        }
    ]
})

# ── planner 응답 (토픽별) ──────────────────────────────────────────────

_MOCK_PLAN_RESPONSES = {
    "Docker": """# Plan: Docker 메모리 설정 및 Health Check 추가
**Date:** 2026-02-28
**Category:** infrastructure
**Priority:** high
**Complexity:** small
**Source:** Discord conversation

## Context
스테이징 서버의 Docker 컨테이너가 OOM(Out of Memory)으로 반복 종료되고 있다. 현재 메모리 limit이 256MB로 설정되어 있으나 Nest.js 서버가 실제로 512MB 이상 사용하며, health check가 없어 컨테이너 장애를 감지하지 못하고 있다.

## Objective
Docker Compose의 메모리 설정을 적정 수준으로 상향하고 liveness/readiness probe를 추가하여 컨테이너 안정성을 확보한다.

## Requirements
- Docker Compose 메모리 limit을 512MB 이상으로 상향
- Liveness probe (HTTP health check endpoint) 추가
- Readiness probe 추가
- Health check 실패 시 자동 재시작 설정

## Technical Specifications
### Tech Stack
- Docker / Docker Compose
- Nest.js (기존 백엔드)

### Architecture
기존 docker-compose.yml에 deploy.resources.limits 및 healthcheck 섹션 추가. Nest.js 서버에 /health 엔드포인트 구현.

## Implementation Steps
1. Nest.js 서버에 `/health` 엔드포인트 추가 (DB 연결, 메모리 사용량 체크)
2. docker-compose.yml에 메모리 limit 512MB 설정
3. docker-compose.yml에 healthcheck 설정 추가 (interval: 30s, timeout: 10s, retries: 3)
4. restart policy를 `unless-stopped`로 설정
5. 스테이징 환경에서 테스트

## Acceptance Criteria
- [ ] /health 엔드포인트가 200 OK 반환
- [ ] Docker 컨테이너 메모리 limit이 512MB로 설정됨
- [ ] Health check가 30초 간격으로 실행됨
- [ ] 컨테이너 비정상 시 자동 재시작 확인

## Claude Code Prompt
```
Implement the following infrastructure change:

Docker 메모리 설정 및 Health Check 추가

Context: 스테이징 서버의 Docker 컨테이너가 OOM으로 반복 종료됨. 메모리 limit 상향과 health check 추가가 필요.

1. src/health/health.controller.ts에 /health 엔드포인트 생성:
   - GET /health → { status: "ok", uptime, memoryUsage, dbConnected }
   - DB 연결 실패 시 503 반환

2. docker-compose.yml 수정:
   - deploy.resources.limits.memory: "512M"
   - healthcheck: test: ["CMD", "curl", "-f", "http://localhost:3000/health"]
   - healthcheck: interval: 30s, timeout: 10s, retries: 3
   - restart: unless-stopped

3. 기존 테스트가 깨지지 않도록 주의
```
""",
    "SSE": """# Plan: 대시보드 실시간 알림 SSE 구현
**Date:** 2026-02-28
**Category:** feature
**Priority:** high
**Complexity:** medium
**Source:** Discord conversation

## Context
사용자 대시보드에 실시간 알림 기능이 필요하다. 단방향 알림이므로 WebSocket보다 가벼운 SSE(Server-Sent Events) 방식을 채택한다. 인증 토큰은 URL 파라미터 대신 커스텀 EventSource 래퍼를 통해 Authorization 헤더로 전달한다.

## Objective
SSE 기반의 실시간 알림 시스템을 구현하여 사용자 대시보드에서 새 알림을 즉시 수신할 수 있도록 한다.

## Requirements
- Nest.js 백엔드에 SSE 알림 엔드포인트 구현
- React 프론트엔드에 커스텀 EventSource 래퍼 구현
- Authorization 헤더를 통한 인증 지원
- 알림 타입: 작업 완료, 새 댓글, 시스템 알림
- 연결 끊김 시 자동 재연결

## Technical Specifications
### Tech Stack
- Backend: Nest.js (@Sse decorator, Observable)
- Frontend: React (custom EventSource hook)
- Authentication: JWT Bearer token

### Architecture
백엔드에서 @Sse() 데코레이터로 SSE 스트림을 생성하고, 프론트엔드에서 커스텀 useSSE 훅으로 이벤트를 수신. fetch API의 ReadableStream을 활용하여 Authorization 헤더를 포함한 SSE 연결 구현.

## Implementation Steps
1. 백엔드: `NotificationSseController` 생성 (`GET /api/notifications/stream`)
2. 백엔드: `NotificationService`에 Observable 기반 이벤트 발행 로직 추가
3. 프론트엔드: `useSSE` 커스텀 훅 구현 (fetch + ReadableStream)
4. 프론트엔드: `NotificationBell` 컴포넌트에 실시간 알림 표시
5. 자동 재연결 로직 구현 (exponential backoff)
6. 단위 테스트 및 통합 테스트 작성

## Acceptance Criteria
- [ ] SSE 엔드포인트가 인증된 사용자에게만 이벤트 스트리밍
- [ ] 프론트엔드에서 실시간 알림 수신 및 UI 표시
- [ ] 연결 끊김 시 자동 재연결 (최대 5회, exponential backoff)
- [ ] 3종 알림 타입 구분 표시 (작업 완료, 댓글, 시스템)

## Claude Code Prompt
```
Implement the following feature:

대시보드 실시간 알림 SSE 구현

Context: 사용자 대시보드에 실시간 알림이 필요. SSE 방식으로 구현하며 JWT 인증 지원.

Backend (Nest.js):
1. src/notifications/notification-sse.controller.ts:
   - @Sse('stream') endpoint returning Observable<MessageEvent>
   - @UseGuards(JwtAuthGuard) for authentication
   - User-specific event filtering by JWT subject

2. src/notifications/notification.service.ts:
   - Subject<NotificationEvent> for event publishing
   - emit(userId, type, payload) method
   - Types: 'task_complete' | 'new_comment' | 'system'

Frontend (React):
3. src/hooks/useSSE.ts:
   - Custom hook using fetch + ReadableStream (not native EventSource)
   - Adds Authorization: Bearer <token> header
   - Auto-reconnect with exponential backoff (1s, 2s, 4s, 8s, 16s)
   - Returns { events, isConnected, error }

4. src/components/NotificationBell.tsx:
   - Badge with unread count
   - Dropdown showing recent notifications
   - Visual distinction by notification type
```
""",
    "리포트": """# Plan: 월간 리포트 API 성능 최적화
**Date:** 2026-02-28
**Category:** bug
**Priority:** medium
**Complexity:** medium
**Source:** Discord conversation

## Context
/api/reports/monthly 엔드포인트의 응답 시간이 3초 이상 소요되어 사용자 경험을 저해하고 있다. TypeORM에서 관계 데이터를 eager loading 없이 조회하여 N+1 쿼리가 발생하고 있으며, 캐싱이 적용되지 않아 매 요청마다 동일한 집계 쿼리가 실행되고 있다.

## Objective
월간 리포트 API 응답 시간을 500ms 이하로 단축한다.

## Requirements
- TypeORM 쿼리 최적화 (N+1 해결)
- Redis 캐시 적용 (TTL 5분)
- 캐시 무효화 전략 수립
- 성능 모니터링을 위한 로깅 추가

## Technical Specifications
### Tech Stack
- Nest.js + TypeORM
- Redis (캐싱)
- PostgreSQL (데이터베이스)

### Architecture
기존 ReportService의 쿼리를 QueryBuilder로 리팩토링하여 JOIN으로 변환. CacheInterceptor를 활용하여 Redis 캐시를 투명하게 적용.

## Implementation Steps
1. 현재 쿼리 분석 및 N+1 지점 식별
2. TypeORM QueryBuilder로 JOIN 기반 쿼리 리팩토링
3. Redis 캐시 모듈 설정 (CacheModule)
4. @CacheKey / @CacheTTL 데코레이터로 엔드포인트 캐싱
5. 데이터 변경 시 캐시 무효화 로직 추가
6. 응답 시간 로깅 미들웨어 추가
7. 성능 테스트 실행 및 목표치(500ms) 확인

## Acceptance Criteria
- [ ] /api/reports/monthly 응답 시간 500ms 이하
- [ ] N+1 쿼리 제거 확인 (SQL 로그 검증)
- [ ] Redis 캐시 히트율 90% 이상
- [ ] 데이터 변경 시 캐시가 정상 무효화됨
- [ ] 기존 API 응답 형식 호환성 유지

## Claude Code Prompt
```
Implement the following bug fix:

월간 리포트 API 성능 최적화

Context: /api/reports/monthly가 3초 이상 소요됨. N+1 쿼리 문제와 캐싱 부재가 원인.

1. src/reports/report.service.ts 수정:
   - findMonthlyReport() 메서드의 개별 쿼리를 QueryBuilder JOIN으로 리팩토링
   - .leftJoinAndSelect()로 관련 엔티티 한 번에 로드
   - .select()로 필요한 컬럼만 조회

2. Redis 캐시 적용:
   - app.module.ts에 CacheModule.register({ store: redisStore, ttl: 300 })
   - report.controller.ts에 @UseInterceptors(CacheInterceptor)
   - @CacheKey('monthly-report') @CacheTTL(300)

3. 캐시 무효화:
   - report.service.ts에서 데이터 변경 시 cacheManager.del('monthly-report')

4. 성능 로깅:
   - 미들웨어에서 요청 시작/종료 시간 측정 및 로깅
```
""",
    "구구단": """# Plan: Go 구구단 CLI 프로그램 개발
**Date:** 2026-03-01
**Category:** feature
**Priority:** medium
**Complexity:** small
**Source:** Discord conversation

## Context
신입 개발자 온보딩 예제로 사용할 Go 구구단 CLI 프로그램이 필요하다. 사용자가 단수를 입력하면 해당 단의 구구단을 출력하는 간단한 프로그램이다.

## Objective
Go 언어로 구구단 CLI 프로그램을 개발하여 신입 온보딩 예제로 활용한다.

## Requirements
- CLI에서 단수를 입력받아 구구단 출력
- 입력 검증 (숫자 여부, 1~9 범위)
- 에러 시 안내 메시지 출력

## Technical Specifications
### Tech Stack
- Go (Golang)
- 표준 라이브러리만 사용 (fmt, os, strconv)

### Architecture
단일 main.go 파일로 구성. fmt.Scan으로 입력받고 for 루프로 결과 출력.

## Implementation Steps
1. data/result/go-gugudan/ 디렉토리 생성
2. main.go 작성: 입력 → 검증 → 출력
3. go mod init으로 모듈 초기화
4. 빌드 및 실행 테스트

## Acceptance Criteria
- [ ] 1~9 범위의 단수 입력 시 정상 출력
- [ ] 범위 밖 숫자 입력 시 경고 메시지
- [ ] 숫자가 아닌 입력 시 안내 메시지
- [ ] go build 성공

## Claude Code Prompt
```
Implement the following feature:

Go 구구단 CLI 프로그램 개발

Context: 신입 온보딩용 Go 구구단 CLI 프로그램. data/result/go-gugudan/ 디렉토리에 개발.

1. data/result/go-gugudan/main.go 생성:
   - fmt.Scan으로 단수(정수) 입력
   - 1~9 범위 검증, 범위 밖이면 "1~9 사이의 숫자를 입력해주세요" 출력
   - 숫자가 아닌 값이면 "숫자를 입력해주세요" 출력
   - for i := 1; i <= 9; i++ 루프로 "N x i = N*i" 형식 출력

2. data/result/go-gugudan/go.mod 생성:
   - module go-gugudan
   - go 1.21
```
"""
}

# ── 키워드 목록 ────────────────────────────────────────────────────────

_DEV_KEYWORDS = ["docker", "api", "bug", "feature", "deploy", "서버", "컨테이너",
                  "엔드포인트", "쿼리", "캐시", "SSE", "WebSocket", "리팩토링",
                  "health check", "OOM", "메모리 limit", "N+1", "TypeORM",
                  "golang", "go", "구구단", "CLI", "프로그램"]


# ── Mock 함수들 ────────────────────────────────────────────────────────

def _mock_call_claude_analyzer(prompt: str, timeout: int = 120) -> str:
    """analyzer용 시뮬레이션 응답 - 개발 관련 키워드가 없으면 빈 토픽 반환."""
    prompt_lower = prompt.lower()
    if not any(kw.lower() in prompt_lower for kw in _DEV_KEYWORDS):
        return '{"dev_topics": []}'
    # 구구단/Go 관련 키워드가 있으면 0301 응답
    go_keywords = ["구구단", "golang", "온보딩", "go로"]
    if any(kw in prompt_lower for kw in go_keywords):
        return _MOCK_ANALYSIS_RESPONSE_0301
    # 그 외 개발 키워드는 0228 응답
    return _MOCK_ANALYSIS_RESPONSE_0228


def _mock_call_claude_planner(prompt: str, timeout: int = 180) -> str:
    """planner용 시뮬레이션 응답 - Title 필드에서 토픽 제목을 매칭."""
    title_match = re.search(r"- Title: (.+)", prompt)
    title = title_match.group(1) if title_match else ""
    for key, response in _MOCK_PLAN_RESPONSES.items():
        if key in title:
            return response
    return list(_MOCK_PLAN_RESPONSES.values())[0]


def _mock_executor_for_gugudan(prompt: str, timeout: int = 300) -> str:
    """executor용 시뮬레이션 - 구구단 plan이면 실제로 Go 파일 생성."""
    result_dir = Path("data/result/go-gugudan")
    result_dir.mkdir(parents=True, exist_ok=True)

    # main.go 생성
    main_go = result_dir / "main.go"
    main_go.write_text('''package main

import (
\t"fmt"
\t"os"
\t"strconv"
)

func main() {
\tfmt.Print("구구단 - 단수를 입력하세요 (1~9): ")

\tvar input string
\tfmt.Scan(&input)

\tnum, err := strconv.Atoi(input)
\tif err != nil {
\t\tfmt.Println("숫자를 입력해주세요.")
\t\tos.Exit(1)
\t}

\tif num < 1 || num > 9 {
\t\tfmt.Println("1~9 사이의 숫자를 입력해주세요.")
\t\tos.Exit(1)
\t}

\tfmt.Printf("\\n=== %d단 ===\\n", num)
\tfor i := 1; i <= 9; i++ {
\t\tfmt.Printf("%d x %d = %d\\n", num, i, num*i)
\t}
}
''', encoding="utf-8")

    # go.mod 생성
    go_mod = result_dir / "go.mod"
    go_mod.write_text("module go-gugudan\n\ngo 1.21\n", encoding="utf-8")

    return "Successfully created Go gugudan CLI program in data/result/go-gugudan/"


# ── 메인 실행 ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    date = sys.argv[1] if len(sys.argv) > 1 else "2026-03-01"

    with patch("analyzer.analyzer._call_claude", side_effect=_mock_call_claude_analyzer), \
         patch("planner.planner._call_claude", side_effect=_mock_call_claude_planner):
        from pipeline import run_pipeline
        run_pipeline(date=date, force=True)

    # 구구단 plan이 생성된 경우 executor 시뮬레이션
    gugudan_plan = Path("data/plans/2026-03-01_go-구구단-cli-프로그램-개발.md")
    if gugudan_plan.exists():
        print("\n[executor 시뮬레이션] 구구단 plan 실행 중...")
        result = _mock_executor_for_gugudan("")
        print(f"[executor 시뮬레이션] {result}")
