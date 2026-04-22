"""
XML/JSON extraction helpers.

Each agent wraps its structured output in a specific XML tag.  These utilities
extract the JSON payload from those tags, validate it against the Pydantic models,
and surface clean error messages when parsing fails.
"""

from __future__ import annotations

import json
import re
from typing import Type, TypeVar

from pydantic import BaseModel, ValidationError

from .models import Assessment, LogicTree, TruthSchema

T = TypeVar("T", bound=BaseModel)


def _extract_xml_content(tag: str, text: str) -> str:
    """Return the inner text of the first matching <tag>…</tag> block."""
    pattern = rf"<{tag}>(.*?)</{tag}>"
    match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
    if not match:
        raise ValueError(
            f"Could not locate <{tag}> … </{tag}> block in the LLM response.\n"
            f"Full response snippet:\n{text[:800]}"
        )
    return match.group(1).strip()


def _parse_json_into(model: Type[T], raw_json: str) -> T:
    """Parse *raw_json* into *model*, raising descriptive errors on failure."""
    # Strip markdown code fences that some models add around JSON.
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw_json.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```$", "", cleaned.strip(), flags=re.MULTILINE)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"JSON decode failed for {model.__name__}.\n"
            f"Error: {exc}\n"
            f"Payload:\n{cleaned[:600]}"
        ) from exc
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise ValueError(
            f"Schema validation failed for {model.__name__}.\n"
            f"Errors: {exc}\n"
            f"Payload:\n{cleaned[:600]}"
        ) from exc


def parse_truth_schema(llm_response: str) -> TruthSchema:
    """Extract and validate the <truth_schema> block from Agent 1's response."""
    raw = _extract_xml_content("truth_schema", llm_response)
    return _parse_json_into(TruthSchema, raw)


def parse_logic_tree(llm_response: str) -> LogicTree:
    """Extract and validate the <logic_tree> block from Agent 2's response."""
    raw = _extract_xml_content("logic_tree", llm_response)
    return _parse_json_into(LogicTree, raw)


def parse_assessment(llm_response: str) -> Assessment:
    """Extract and validate the <assessment> block from Agent 3's response."""
    raw = _extract_xml_content("assessment", llm_response)
    return _parse_json_into(Assessment, raw)
