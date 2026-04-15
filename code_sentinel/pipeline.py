"""
CodeSentinelPipeline — orchestrates the three-agent sequential loop.

State flows as:
    PipelineState (inputs)
        → ContextCartographer  → PipelineState (+ truth_schema)
        → LogicTranslator      → PipelineState (+ logic_tree)
        → AdversarialAuditor   → PipelineState (+ assessment)
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from openai import OpenAI

from .agents import AdversarialAuditor, ContextCartographer, LogicTranslator
from .models import PipelineState

logger = logging.getLogger(__name__)

# Type alias for an optional hook called after each agent completes.
StepHook = Callable[[str, PipelineState], None]


class CodeSentinelPipeline:
    """
    Sequential three-agent pipeline with optional per-step hooks for
    human-in-the-loop review or observability tooling.

    Parameters
    ----------
    model:
        OpenAI model identifier passed to all three agents.
    client:
        Optional pre-configured OpenAI client (useful for testing / mocking).
    on_step_complete:
        Optional callback invoked after each agent finishes.
        Receives the agent name and the current PipelineState.
    """

    def __init__(
        self,
        model: str = "gpt-4o",
        client: Optional[OpenAI] = None,
        on_step_complete: Optional[StepHook] = None,
    ) -> None:
        self.cartographer = ContextCartographer(model=model, client=client)
        self.translator = LogicTranslator(model=model, client=client)
        self.auditor = AdversarialAuditor(model=model, client=client)
        self._on_step_complete = on_step_complete

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        prd: str,
        technical_spec: str,
        source_code: str,
    ) -> PipelineState:
        """
        Execute the full three-step pipeline and return the final PipelineState.

        Raises
        ------
        RuntimeError
            If any agent fails to parse its structured output.
        """
        state = PipelineState(
            prd=prd,
            technical_spec=technical_spec,
            source_code=source_code,
        )

        # Step 1 — Context Cartographer
        logger.info("[Pipeline] Step 1/3 — Context Cartographer starting…")
        state = self._run_step("ContextCartographer", self.cartographer.run, state)

        # Step 2 — Logic Translator
        logger.info("[Pipeline] Step 2/3 — Logic Translator starting…")
        state = self._run_step("LogicTranslator", self.translator.run, state)

        # Step 3 — Adversarial Auditor
        logger.info("[Pipeline] Step 3/3 — Adversarial Auditor starting…")
        state = self._run_step("AdversarialAuditor", self.auditor.run, state)

        logger.info(
            "[Pipeline] Complete. Compliance score: %s | Hard block: %s",
            state.assessment.compliance_score if state.assessment else "N/A",
            state.assessment.hard_block if state.assessment else "N/A",
        )
        return state

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_step(
        self,
        name: str,
        fn: Callable[[PipelineState], PipelineState],
        state: PipelineState,
    ) -> PipelineState:
        """Run a single agent step, fire the optional hook, and return state."""
        state = fn(state)
        if self._on_step_complete:
            self._on_step_complete(name, state)
        return state
