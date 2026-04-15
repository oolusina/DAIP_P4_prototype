"""
The three agents that comprise The Code Sentinel pipeline.

Each agent wraps a single LLM call, injects the appropriate system prompt,
and parses + validates the structured XML/JSON output before returning it.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from openai import OpenAI

from .models import Assessment, LogicTree, PipelineState, TruthSchema
from .parsers import parse_assessment, parse_logic_tree, parse_truth_schema

# ---------------------------------------------------------------------------
# System prompts (verbatim from spec, stored as module-level constants so
# they can be patched or overridden in tests without subclassing)
# ---------------------------------------------------------------------------

CARTOGRAPHER_SYSTEM_PROMPT = """\
You are the Context Cartographer. Your mission is to establish the 'Ground Truth' \
for a technical project by synthesizing unstructured documentation into a \
machine-readable validation schema.

### I. OPERATIONAL PROTOCOL
1. **Deconstruct Documents:** Analyze the provided <prd> and <technical_spec> tags.
2. **Extract Hard Constraints:** Identify quantifiable limits (e.g., 'Latency < 200ms', \
'Memory usage < 50MB').
3. **Identify Soft Requirements:** Capture qualitative goals \
(e.g., 'User experience must feel snappy').
4. **Conflict Mapping:** Highlight contradictory requirements within the PRD itself.

### II. OUTPUT SCHEMA
You must wrap your output in <truth_schema> tags using the following JSON structure:
```json
{
  "constraints": [{"id": "REQ-01", "type": "hard", "description": "", "metric": ""}],
  "product_goals": [{"id": "GOAL-01", "description": "", "priority": "P0|P1"}],
  "tensions": ["Requirement A conflicts with Constraint B"],
  "missing_info": ["Topics not specified in the source documents"]
}
```

### III. GUARDRAILS
- NEVER infer a requirement that is not explicitly stated or logically necessitated \
by a stated goal.
- If information is missing (e.g., no latency specified), flag it in the \
`missing_info` array within the JSON block.
- DO NOT comment on code quality.
- Your response MUST contain a <truth_schema>…</truth_schema> XML block with valid JSON inside.
"""

TRANSLATOR_SYSTEM_PROMPT = """\
You are the Logic Translator. You act as a language-agnostic interpreter that bridges \
raw source code and high-level architectural intent.

### I. ANALYSIS STEPS
1. **Syntax Neutralization:** Ignore variable names and 'syntactic sugar.' \
Focus on the Control Flow Graph (CFG).
2. **I/O Mapping:** Identify all entry points (API calls, UI events) and exit points \
(DB writes, network requests).
3. **Dependency Auditing:** List all third-party libraries and their specific usage context.

### II. LOGIC REPRESENTATION (IR)
Provide a structured summary in <logic_tree> tags with valid JSON:
```json
{
  "primary_intent": "What is the atomic goal of this code?",
  "execution_path": ["Step 1", "Step 2", "Step 3"],
  "entry_points": ["API endpoint / UI event"],
  "exit_points": ["DB write / network request"],
  "dependencies": ["library — usage context"],
  "data_vulnerabilities": ["Description of where raw data is handled without validation"],
  "uncertainty_nodes": [{"location": "module::function", "reason": "async race / AI call / RNG"}]
}
```

### III. CRITICAL RULES
- If the code is non-deterministic (uses AI, random seeds, or async races), you MUST \
flag this in an uncertainty_nodes entry.
- Do not suggest fixes. Only describe 'What IS.'
- Use technical, dry terminology.
- Your response MUST contain a <logic_tree>…</logic_tree> XML block with valid JSON inside.
"""

AUDITOR_SYSTEM_PROMPT = """\
You are the Adversarial Auditor. You are the final arbiter of 'Deployability.' \
You evaluate code through two competing lenses: Product Utility and Safety/Responsibility.

### I. THE EVALUATION LOOP
1. **Goal Alignment:** Does the <logic_tree> fulfill the <truth_schema>?
2. **Safety Benchmarking:** Evaluate the logic against global standards \
(OWASP, NIST AI RMF).
3. **Tension Analysis:** Identify where Product Success (e.g., Performance) \
creates Safety Risk (e.g., Security bypass).

### II. REQUIRED OUTPUT STRUCTURE
Wrap your final decision in <assessment> tags with valid JSON:
```json
{
  "compliance_score": 0,
  "hard_block": false,
  "requirement_status": [
    {"req_id": "REQ-01", "status": "Met|Failed|Partial", "rationale": ""}
  ],
  "tension_report": [
    {"description": "", "product_impact": "", "safety_impact": ""}
  ],
  "golden_path": "Specific code modification or architectural change that resolves tensions.",
  "summary": "One-paragraph executive summary."
}
```

### III. ADVERSARIAL MODE
- Be hyper-skeptical. If code looks 'too simple' to be secure, assume it is insecure.
- **Red Flag Trigger:** If the code uses legacy endpoints for speed, set \
"hard_block": true in the JSON output.
- Your response MUST contain an <assessment>…</assessment> XML block with valid JSON inside.
"""


# ---------------------------------------------------------------------------
# Base LLM client helper
# ---------------------------------------------------------------------------

def _get_client() -> OpenAI:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "OPENAI_API_KEY environment variable is not set. "
            "Add it via Cursor Dashboard > Cloud Agents > Secrets."
        )
    return OpenAI(api_key=api_key)


def _chat(
    client: OpenAI,
    system_prompt: str,
    user_message: str,
    model: str = "gpt-4o",
    temperature: float = 0.2,
) -> str:
    """Single-turn chat call. Returns the raw assistant content string."""
    response = client.chat.completions.create(
        model=model,
        temperature=temperature,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
    )
    return response.choices[0].message.content or ""


# ---------------------------------------------------------------------------
# Agent 1 — Context Cartographer
# ---------------------------------------------------------------------------

class ContextCartographer:
    """Converts PRD + technical spec documents into a validated TruthSchema."""

    def __init__(self, model: str = "gpt-4o", client: Optional[OpenAI] = None) -> None:
        self.model = model
        self._client = client

    @property
    def client(self) -> OpenAI:
        if self._client is None:
            self._client = _get_client()
        return self._client

    def run(self, state: PipelineState) -> PipelineState:
        """
        Execute Chain-of-Thought extraction on the PRD/spec documents.

        Mutates *state* in-place by populating `truth_schema` and
        `raw_cartographer_response`, then returns it.
        """
        user_message = (
            f"<prd>\n{state.prd}\n</prd>\n\n"
            f"<technical_spec>\n{state.technical_spec}\n</technical_spec>\n\n"
            "Think step-by-step through each OPERATIONAL PROTOCOL step before "
            "producing the output schema."
        )

        raw = _chat(self.client, CARTOGRAPHER_SYSTEM_PROMPT, user_message, self.model)
        state.raw_cartographer_response = raw

        try:
            state.truth_schema = parse_truth_schema(raw)
        except ValueError as exc:
            raise RuntimeError(
                f"[ContextCartographer] Failed to parse Truth Schema.\n{exc}"
            ) from exc

        return state


# ---------------------------------------------------------------------------
# Agent 2 — Logic Translator
# ---------------------------------------------------------------------------

class LogicTranslator:
    """Converts source code into a language-agnostic LogicTree IR."""

    def __init__(self, model: str = "gpt-4o", client: Optional[OpenAI] = None) -> None:
        self.model = model
        self._client = client

    @property
    def client(self) -> OpenAI:
        if self._client is None:
            self._client = _get_client()
        return self._client

    def run(self, state: PipelineState) -> PipelineState:
        """
        Perform CFG / I/O / dependency analysis on the source code.

        Mutates *state* in-place by populating `logic_tree` and
        `raw_translator_response`, then returns it.
        """
        user_message = (
            "Analyze the following source code. "
            "Think step-by-step through each ANALYSIS STEP before producing the IR.\n\n"
            f"```\n{state.source_code}\n```"
        )

        raw = _chat(self.client, TRANSLATOR_SYSTEM_PROMPT, user_message, self.model)
        state.raw_translator_response = raw

        try:
            state.logic_tree = parse_logic_tree(raw)
        except ValueError as exc:
            raise RuntimeError(
                f"[LogicTranslator] Failed to parse Logic Tree.\n{exc}"
            ) from exc

        return state


# ---------------------------------------------------------------------------
# Agent 3 — Adversarial Auditor
# ---------------------------------------------------------------------------

class AdversarialAuditor:
    """Cross-examines the LogicTree against the TruthSchema and issues an Assessment."""

    def __init__(self, model: str = "gpt-4o", client: Optional[OpenAI] = None) -> None:
        self.model = model
        self._client = client

    @property
    def client(self) -> OpenAI:
        if self._client is None:
            self._client = _get_client()
        return self._client

    def run(self, state: PipelineState) -> PipelineState:
        """
        Perform ReAct-style reasoning over the schema + IR and emit a deployment verdict.

        Mutates *state* in-place by populating `assessment` and
        `raw_auditor_response`, then returns it.
        """
        if state.truth_schema is None:
            raise ValueError(
                "[AdversarialAuditor] truth_schema is None — "
                "run ContextCartographer first."
            )
        if state.logic_tree is None:
            raise ValueError(
                "[AdversarialAuditor] logic_tree is None — "
                "run LogicTranslator first."
            )

        user_message = (
            "You have been provided with two structured artifacts.\n\n"
            f"<truth_schema>\n{state.truth_schema.model_dump_json(indent=2)}\n</truth_schema>\n\n"
            f"<logic_tree>\n{state.logic_tree.model_dump_json(indent=2)}\n</logic_tree>\n\n"
            "Think step-by-step through each EVALUATION LOOP step, then produce "
            "your final <assessment> block."
        )

        raw = _chat(self.client, AUDITOR_SYSTEM_PROMPT, user_message, self.model)
        state.raw_auditor_response = raw

        try:
            state.assessment = parse_assessment(raw)
        except ValueError as exc:
            raise RuntimeError(
                f"[AdversarialAuditor] Failed to parse Assessment.\n{exc}"
            ) from exc

        return state
