"""Analyzes Discord conversation files to extract development-related topics."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from shared.claude_cli import call_claude as _call_claude

logger = logging.getLogger(__name__)


@dataclass
class Message:
    author: str
    content: str
    timestamp: str


@dataclass
class DevTopic:
    id: str
    title: str
    category: str  # feature|bug|refactor|infrastructure|discussion
    priority: str  # high|medium|low
    messages: list[Message]
    summary: str
    keywords: list[str]
    actionable: bool
    estimated_complexity: str  # small|medium|large


@dataclass
class AnalysisResult:
    date: str
    analyzed_at: str
    source_files: list[str]
    dev_topics: list[DevTopic]
    total_messages_analyzed: int
    dev_topics_found: int


_ANALYSIS_PROMPT = """You are analyzing Discord messages to identify software development tasks and requests.

These messages may come from text chat OR voice transcriptions (speech-to-text).
Voice transcriptions often contain recognition errors, informal speech, and single-person monologues
where someone describes what they want to build. Treat these as valid development requests.

Analyze the following messages and extract ANY topic related to software development.

Look for:
- Feature requests and new feature ideas
- Bug reports and fixes needed
- Architecture and design discussions
- Infrastructure and DevOps topics
- Technical debt and refactoring opportunities
- Any concrete development task that could be implemented by a developer
- Direct requests or instructions to build/create/implement something
- Even a single person describing what they want to develop counts as a valid topic

IMPORTANT GUIDELINES:
- ALL output fields MUST be written in English, even if the conversation is in Korean or another language.
- Voice transcription errors are common (e.g. misheard words). Infer the intended meaning from context.
- A single person giving development instructions IS a valid development topic — it does NOT need to be a multi-person discussion.
- When in doubt, mark a topic as actionable. It is better to capture a borderline topic than to miss a real one.

For each dev topic found, provide:
- title: concise English title (under 60 chars, e.g. "Add Docker Health Check" not "Add Docker Health Check in Korean")
- category: one of [feature, bug, refactor, infrastructure, discussion]
- priority: one of [high, medium, low]
- summary: 2-3 sentences in English explaining what was discussed and why it matters
- keywords: 3-7 relevant technical keywords in English
- actionable: true if this could be directly implemented, false if just casual discussion
- estimated_complexity: one of [small, medium, large]
- relevant_message_indices: list of 0-based indices of messages that belong to this topic

Respond ONLY with valid JSON in this exact format:
{{
  "dev_topics": [
    {{
      "title": "...",
      "category": "feature",
      "priority": "high",
      "summary": "...",
      "keywords": ["keyword1", "keyword2"],
      "actionable": true,
      "estimated_complexity": "medium",
      "relevant_message_indices": [0, 1, 3]
    }}
  ]
}}

If no development topics are found, return: {{"dev_topics": []}}

Messages to analyze:
{conversation}
"""

_MAX_MESSAGES_PER_CHUNK = 100
_MAX_CHARS_PER_CHUNK = 8000


def _load_conversation_file(path: Path) -> dict:
    """Load a conversation file as JSON. Returns an empty dict if file is missing or parsing fails."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                logger.warning("Conversation file is not a dict: %s", path)
                return {}
            return data
    except FileNotFoundError:
        logger.error("Conversation file not found: %s", path)
        return {}
    except json.JSONDecodeError as e:
        logger.error("Failed to parse conversation file as JSON: %s (%s)", path, e)
        return {}
    except OSError as e:
        logger.error("Failed to read conversation file: %s (%s)", path, e)
        return {}


def _format_messages_for_prompt(messages: list[dict]) -> str:
    lines = []
    for i, msg in enumerate(messages):
        ts = msg.get("timestamp", "")[:19].replace("T", " ")
        lines.append(f"[{i}] {msg['author']} ({ts}): {msg['content']}")
    return "\n".join(lines)


def _chunk_messages(messages: list[dict]) -> list[list[dict]]:
    """Split a long conversation into analyzable chunks."""
    chunks: list[list[dict]] = []
    current: list[dict] = []
    current_chars = 0

    for msg in messages:
        content_len = len(msg.get("content", ""))
        if (
            current
            and (len(current) >= _MAX_MESSAGES_PER_CHUNK or current_chars + content_len > _MAX_CHARS_PER_CHUNK)
        ):
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(msg)
        current_chars += content_len

    if current:
        chunks.append(current)
    return chunks


def _parse_claude_response(response_text: str) -> list[dict]:
    """Parse JSON from a Claude response."""
    # Extract only the JSON block (handle markdown code fences)
    match = re.search(r"\{[\s\S]*\}", response_text)
    if not match:
        return []
    try:
        data = json.loads(match.group())
        return data.get("dev_topics", [])
    except json.JSONDecodeError:
        return []


def _deduplicate_topics(topics: list[DevTopic]) -> list[DevTopic]:
    """Remove duplicate topics with similar titles."""
    seen_titles: set[str] = set()
    result: list[DevTopic] = []
    for topic in topics:
        normalized = topic.title.lower().strip()
        if normalized not in seen_titles:
            seen_titles.add(normalized)
            result.append(topic)
    return result


def analyze_conversations(files: list[Path], date: str, data_dir: Path | None = None) -> AnalysisResult:
    """Analyze conversation files to extract development topics.

    Args:
        files: List of conversation JSON files to analyze.
        date: Analysis date (YYYY-MM-DD).
        data_dir: Root data directory. Defaults to Path("data") if None.

    Returns:
        AnalysisResult: Analysis result containing extracted development topics.
    """
    all_topics: list[DevTopic] = []
    source_files: list[str] = []
    total_messages = 0
    topic_counter = 0

    for conv_file in files:
        source_files.append(str(conv_file))
        data = _load_conversation_file(conv_file)
        messages: list[dict] = data.get("messages", [])
        total_messages += len(messages)

        # Remove messages with no content (bot messages, empty messages, etc.)
        messages = [m for m in messages if m.get("content", "").strip()]

        for chunk in _chunk_messages(messages):
            conversation_text = _format_messages_for_prompt(chunk)
            prompt = _ANALYSIS_PROMPT.format(conversation=conversation_text)

            try:
                response_text = _call_claude(prompt)
            except Exception as e:
                logger.error("Claude CLI call failed (file: %s): %s", conv_file.name, e)
                continue
            raw_topics = _parse_claude_response(response_text)

            for raw in raw_topics:
                topic_counter += 1
                indices = raw.get("relevant_message_indices", [])
                relevant_msgs = [
                    Message(
                        author=chunk[i]["author"],
                        content=chunk[i]["content"],
                        timestamp=chunk[i].get("timestamp", ""),
                    )
                    for i in indices
                    if 0 <= i < len(chunk)
                ]
                all_topics.append(
                    DevTopic(
                        id=f"topic_{topic_counter:03d}",
                        title=raw.get("title", "Untitled"),
                        category=raw.get("category", "discussion"),
                        priority=raw.get("priority", "medium"),
                        messages=relevant_msgs,
                        summary=raw.get("summary", ""),
                        keywords=raw.get("keywords", []),
                        actionable=raw.get("actionable", False),
                        estimated_complexity=raw.get("estimated_complexity", "medium"),
                    )
                )

    unique_topics = _deduplicate_topics(all_topics)
    result = AnalysisResult(
        date=date,
        analyzed_at=datetime.now(timezone.utc).isoformat(),
        source_files=source_files,
        dev_topics=unique_topics,
        total_messages_analyzed=total_messages,
        dev_topics_found=len(unique_topics),
    )

    # Save analysis result
    if data_dir is None:
        data_dir = Path("data")
    analysis_dir = data_dir / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    output_path = analysis_dir / f"{date}_analysis.json"

    def _serialize(obj):
        if hasattr(obj, "__dict__"):
            return obj.__dict__
        return str(obj)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result.__dict__, f, ensure_ascii=False, indent=2, default=_serialize)

    logger.info("Analysis complete: %d topic(s) found -> %s", len(unique_topics), output_path)
    return result
