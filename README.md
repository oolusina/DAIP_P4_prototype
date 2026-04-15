# The Code Sentinel

A production-grade, three-agent AI pipeline for code review and requirements validation.

## Architecture

```
PipelineState (PRD + Tech Spec + Source Code)
        │
        ▼
┌───────────────────────┐
│  Context Cartographer │  Agent 1 — Extracts a validated Truth Schema from docs
│  (Agent 1)            │  Output: <truth_schema> JSON (constraints, goals, tensions)
└──────────┬────────────┘
           │ TruthSchema
           ▼
┌───────────────────────┐
│  Logic Translator     │  Agent 2 — Produces a language-agnostic IR from source code
│  (Agent 2)            │  Output: <logic_tree> JSON (CFG, I/O map, vulnerabilities)
└──────────┬────────────┘
           │ LogicTree
           ▼
┌───────────────────────┐
│  Adversarial Auditor  │  Agent 3 — Cross-examines IR vs. schema → deployment verdict
│  (Agent 3)            │  Output: <assessment> JSON (score, requirement status, golden path)
└───────────────────────┘
```

## Agent Design Patterns

| Agent | Design Pattern | Key Output Tag |
|---|---|---|
| Context Cartographer | XML Tagging + CoT | `<truth_schema>` |
| Logic Translator | Intermediate Representation (IR) | `<logic_tree>` |
| Adversarial Auditor | ReAct (Reason + Act) | `<assessment>` |

## Key Features

- **Structured schemas** — All inter-agent data uses Pydantic v2 models with strict validation; no freeform text crosses agent boundaries.
- **Stable IDs** — Requirements (`REQ-01`) and goals (`GOAL-01`) persist through the pipeline, enabling thread memory and human-in-the-loop tracing.
- **CoT triggers** — Each agent is explicitly instructed to think step-by-step before emitting structured output.
- **Explicit error-handling** — XML extraction and JSON parsing failures surface as descriptive `RuntimeError`s with the offending payload included.
- **Human-in-the-loop hooks** — `CodeSentinelPipeline` accepts an `on_step_complete` callback invoked after each agent, making it easy to plug in a review UI.
- **Hard Block** — The Adversarial Auditor can emit `"hard_block": true` to programmatically halt a deployment pipeline.

## Project Layout

```
code_sentinel/
├── __init__.py      # Public API surface
├── models.py        # Pydantic schemas (TruthSchema, LogicTree, Assessment, PipelineState)
├── parsers.py       # XML/JSON extraction utilities
├── agents.py        # The three agent classes + system prompts
└── pipeline.py      # CodeSentinelPipeline orchestrator
main.py              # Example entry point with a deliberately insecure Flask app
requirements.txt
```

## Quickstart

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Set your API key

```bash
export OPENAI_API_KEY=sk-...
```

Or create a `.env` file:

```
OPENAI_API_KEY=sk-...
```

### 3. Run the example

```bash
python main.py
```

The example ships with a deliberately insecure Flask app (SQL injection, MD5 passwords, hardcoded secrets) so you can see the Adversarial Auditor trigger a **HARD BLOCK** out of the box.

## Programmatic Usage

```python
from code_sentinel import CodeSentinelPipeline

def my_hook(agent_name, state):
    print(f"{agent_name} done — score so far: {state.assessment}")

pipeline = CodeSentinelPipeline(
    model="gpt-4o",
    on_step_complete=my_hook,
)

state = pipeline.run(
    prd="...",
    technical_spec="...",
    source_code="...",
)

assessment = state.assessment
print(f"Compliance score : {assessment.compliance_score}/100")
print(f"Hard block       : {assessment.hard_block}")
print(f"Golden path      : {assessment.golden_path}")
```

## Extending to LangGraph / CrewAI / AutoGen

The `PipelineState` Pydantic model is the canonical shared state. Each agent's `.run(state)` method is a pure function with a typed input and output, making it straightforward to wrap as a LangGraph node, a CrewAI task, or an AutoGen conversable agent:

```python
# LangGraph example
from langgraph.graph import StateGraph
from code_sentinel.agents import ContextCartographer, LogicTranslator, AdversarialAuditor
from code_sentinel.models import PipelineState

graph = StateGraph(PipelineState)
graph.add_node("cartographer", ContextCartographer().run)
graph.add_node("translator",   LogicTranslator().run)
graph.add_node("auditor",      AdversarialAuditor().run)
graph.add_edge("cartographer", "translator")
graph.add_edge("translator",   "auditor")
graph.set_entry_point("cartographer")
app = graph.compile()
```
