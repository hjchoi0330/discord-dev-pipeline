"""shared/plan_format 모듈 단위 테스트.

stage 간 공유 포맷 계약이 일관되게 유지되는지 검증합니다.
"""

from shared.plan_format import PLAN_PROMPT_HEADING, PLAN_PROMPT_SECTION


class TestPlanFormatConstants:
    def test_section_name_is_string(self):
        assert isinstance(PLAN_PROMPT_SECTION, str)
        assert len(PLAN_PROMPT_SECTION) > 0

    def test_heading_starts_with_h2(self):
        assert PLAN_PROMPT_HEADING.startswith("## ")

    def test_heading_contains_section(self):
        assert PLAN_PROMPT_SECTION in PLAN_PROMPT_HEADING

    def test_heading_is_consistent(self):
        """heading은 항상 '## ' + section 이름이어야 합니다."""
        assert PLAN_PROMPT_HEADING == f"## {PLAN_PROMPT_SECTION}"


class TestPlanFormatIntegration:
    """planner와 executor가 같은 계약을 사용하는지 검증합니다."""

    def test_planner_uses_shared_constant(self):
        """planner의 프롬프트 템플릿이 공유 상수를 사용합니다."""
        from planner.planner import PLAN_PROMPT_HEADING as planner_heading
        assert planner_heading == PLAN_PROMPT_HEADING

    def test_executor_uses_shared_constant(self):
        """executor의 프롬프트 추출이 공유 상수를 사용합니다."""
        from executor.executor import PLAN_PROMPT_SECTION as executor_section
        assert executor_section == PLAN_PROMPT_SECTION

    def test_executor_can_parse_planner_output(self):
        """planner가 생성하는 포맷을 executor가 올바르게 파싱합니다."""
        from executor.executor import _extract_prompt

        plan_text = f"""# Plan: Test Feature

## Context
Some context here.

{PLAN_PROMPT_HEADING}
```
Implement the following feature:
Add a test feature with proper error handling.
```

## Notes
Additional notes.
"""
        prompt = _extract_prompt(plan_text)
        assert "Implement the following feature" in prompt
        assert "Add a test feature" in prompt
        assert "Notes" not in prompt
