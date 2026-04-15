"""
The Code Sentinel — a three-agent pipeline for production-grade code review.

Agents
------
1. Context Cartographer  — extracts a validated truth schema from PRD / tech-spec docs.
2. Logic Translator      — produces a language-agnostic IR (logic tree) from source code.
3. Adversarial Auditor   — cross-examines the IR against the schema and emits a deployment verdict.
"""

from .models import (
    Constraint,
    ProductGoal,
    TruthSchema,
    LogicTree,
    Assessment,
    PipelineState,
)
from .pipeline import CodeSentinelPipeline

__all__ = [
    "Constraint",
    "ProductGoal",
    "TruthSchema",
    "LogicTree",
    "Assessment",
    "PipelineState",
    "CodeSentinelPipeline",
]
