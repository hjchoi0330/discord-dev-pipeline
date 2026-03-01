"""Shared format contract between pipeline stages.

Format constants used for data exchange between stages are defined here
so that each stage can operate independently.

This ensures:
- Earlier steps (planner) do not directly reference later steps (executor)
- Format changes require edits in only one place
- Inter-stage contracts are explicitly documented
"""

# Section name in plan markdown that contains the Claude Code execution prompt
PLAN_PROMPT_SECTION = "Claude Code Prompt"

# Full string used as the markdown heading
PLAN_PROMPT_HEADING = f"## {PLAN_PROMPT_SECTION}"
