"""Pipeline stage 간 공유 포맷 계약.

각 stage가 독립적으로 동작할 수 있도록, stage 간 데이터 교환에 사용되는
포맷 상수를 이 모듈에 정의합니다.

이를 통해:
- 앞 step(planner)이 뒤 step(executor)의 구현을 직접 참조하지 않음
- 포맷 변경 시 한 곳만 수정하면 됨
- stage 간 계약(contract)이 명시적으로 문서화됨
"""

# Plan 마크다운에서 Claude Code 실행 프롬프트를 포함하는 섹션 이름
PLAN_PROMPT_SECTION = "Claude Code Prompt"

# 마크다운 헤딩으로 사용할 전체 문자열
PLAN_PROMPT_HEADING = f"## {PLAN_PROMPT_SECTION}"
