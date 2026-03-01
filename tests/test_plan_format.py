"""Unit tests for the shared/plan_format module.

Verifies that the shared format contract between stages is maintained consistently.
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
        """The heading must always be '## ' + the section name."""
        assert PLAN_PROMPT_HEADING == f"## {PLAN_PROMPT_SECTION}"


class TestPlanFormatIntegration:
    """Verifies that planner and executor use the same contract."""

    def test_planner_uses_shared_constant(self):
        """The planner's prompt template uses the shared constant."""
        from planner.planner import PLAN_PROMPT_HEADING as planner_heading
        assert planner_heading == PLAN_PROMPT_HEADING

    def test_executor_uses_shared_constant(self):
        """The executor's prompt extraction uses the shared constant."""
        from executor.executor import PLAN_PROMPT_SECTION as executor_section
        assert executor_section == PLAN_PROMPT_SECTION

    def test_executor_can_parse_planner_output(self):
        """The executor correctly parses the format produced by the planner."""
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
