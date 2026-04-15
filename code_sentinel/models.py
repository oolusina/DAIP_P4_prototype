"""Pydantic models that define the shared state flowing between the three agents."""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Agent 1 output — Truth Schema
# ---------------------------------------------------------------------------

class ConstraintType(str, Enum):
    hard = "hard"
    soft = "soft"


class Priority(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


class Constraint(BaseModel):
    id: str = Field(..., description="Stable identifier, e.g. 'REQ-01'")
    type: ConstraintType
    description: str
    metric: str = Field(default="", description="Quantifiable measure, if applicable")


class ProductGoal(BaseModel):
    id: str = Field(..., description="Stable identifier, e.g. 'GOAL-01'")
    description: str
    priority: Priority = Priority.P1


class TruthSchema(BaseModel):
    """Structured ground truth produced by the Context Cartographer."""

    constraints: List[Constraint] = Field(default_factory=list)
    product_goals: List[ProductGoal] = Field(default_factory=list)
    tensions: List[str] = Field(
        default_factory=list,
        description="Conflicting requirement pairs identified in the source docs.",
    )
    missing_info: List[str] = Field(
        default_factory=list,
        description="Topics that are referenced but not specified in the docs.",
    )


# ---------------------------------------------------------------------------
# Agent 2 output — Logic Tree (Intermediate Representation)
# ---------------------------------------------------------------------------

class UncertaintyNode(BaseModel):
    location: str = Field(..., description="File / function where uncertainty was found")
    reason: str = Field(..., description="Why this is flagged (async race, AI call, RNG, …)")


class LogicTree(BaseModel):
    """Language-agnostic IR produced by the Logic Translator."""

    primary_intent: str
    execution_path: List[str] = Field(
        ...,
        description="Ordered steps of the dominant control-flow path.",
    )
    entry_points: List[str] = Field(default_factory=list)
    exit_points: List[str] = Field(default_factory=list)
    dependencies: List[str] = Field(
        default_factory=list,
        description="Third-party libraries and their usage context.",
    )
    data_vulnerabilities: List[str] = Field(default_factory=list)
    uncertainty_nodes: List[UncertaintyNode] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Agent 3 output — Assessment
# ---------------------------------------------------------------------------

class RequirementStatus(str, Enum):
    Met = "Met"
    Failed = "Failed"
    Partial = "Partial"


class RequirementResult(BaseModel):
    req_id: str
    status: RequirementStatus
    rationale: str = ""


class TensionEntry(BaseModel):
    description: str
    product_impact: str
    safety_impact: str


class Assessment(BaseModel):
    """Final deployment verdict produced by the Adversarial Auditor."""

    compliance_score: int = Field(..., ge=0, le=100)
    hard_block: bool = Field(
        default=False,
        description="True if a HARD BLOCK condition was triggered.",
    )
    requirement_status: List[RequirementResult] = Field(default_factory=list)
    tension_report: List[TensionEntry] = Field(default_factory=list)
    golden_path: str = Field(
        default="",
        description="Specific architectural or code-level recommendation to resolve tensions.",
    )
    summary: str = ""


# ---------------------------------------------------------------------------
# Shared pipeline state
# ---------------------------------------------------------------------------

class PipelineState(BaseModel):
    """End-to-end state object passed through the three-agent pipeline."""

    # Inputs
    prd: str = Field(default="", description="Raw Product Requirements Document text")
    technical_spec: str = Field(default="", description="Raw technical specification text")
    source_code: str = Field(default="", description="Source code to be reviewed")

    # Intermediate + final outputs
    truth_schema: Optional[TruthSchema] = None
    logic_tree: Optional[LogicTree] = None
    assessment: Optional[Assessment] = None

    # Audit trail
    raw_cartographer_response: str = ""
    raw_translator_response: str = ""
    raw_auditor_response: str = ""
