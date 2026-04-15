"""
Flask server for The Code Sentinel demo UI.

Endpoints
---------
POST /api/analyze          Run the real pipeline (requires OPENAI_API_KEY).
POST /api/analyze/mock     Return deterministic fake data — no LLM needed.
GET  /                     Serve the single-page frontend.
GET  /frontend/<path>      Serve static assets.

Both analyze endpoints stream Server-Sent Events so the browser can render
each agent's result as it arrives, without waiting for the full pipeline.

SSE event format:  data: <json>\n\n
Event types (in the "event" field of the JSON payload):
  step_start    — an agent has started       { agent, step }
  step_done     — an agent has finished      { agent, step, payload }
  test_cases    — generated test-case list   { test_cases }
  error         — pipeline error             { message }
  done          — pipeline complete
"""

from __future__ import annotations

import json
import os
import time
from typing import Generator

from dotenv import load_dotenv
from flask import Flask, Response, request, send_from_directory

load_dotenv()

app = Flask(__name__, static_folder="frontend", static_url_path="")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sse(payload: dict) -> str:
    """Format a dict as a single SSE message."""
    return f"data: {json.dumps(payload)}\n\n"


def _derive_test_cases(
    truth_schema: dict,
    logic_tree: dict,
    assessment: dict,
) -> list[dict]:
    """
    Convert pipeline outputs into runnable pytest-style test-case stubs.

    Each test case carries:
      id          — stable identifier
      title       — human-readable name
      category    — Requirements | Security | Performance | Integration
      severity    — critical | high | medium | low
      description — what to verify
      stub        — a Python pytest stub the developer can paste and fill in
      status      — always 'pending' at generation time
    """
    cases: list[dict] = []

    # --- from constraints (Agent 1) ---
    for c in truth_schema.get("constraints", []):
        req_id = c.get("id", "REQ-??")
        metric = c.get("metric", "")
        desc = c.get("description", "")
        severity = "critical" if c.get("type") == "hard" else "medium"
        category = "Performance" if any(k in desc.lower() for k in ("latency", "ms", "throughput", "memory", "cpu")) else "Requirements"

        stub = f'''\
def test_{req_id.lower().replace("-", "_")}():
    """Auto-generated from {req_id}: {desc}"""
    # TODO: instrument your system and assert the metric below
    # Expected: {metric or desc}
    result = invoke_system_under_test()
    assert result  # replace with real assertion
'''
        cases.append({
            "id": f"TC-{req_id}",
            "title": desc[:80],
            "category": category,
            "severity": severity,
            "description": f"{req_id} — {desc}" + (f" ({metric})" if metric else ""),
            "stub": stub,
            "status": "pending",
        })

    # --- from data vulnerabilities (Agent 2) ---
    for i, vuln in enumerate(logic_tree.get("data_vulnerabilities", []), start=1):
        tc_id = f"TC-SEC-{i:02d}"
        stub = f'''\
def test_security_{i:02d}():
    """Auto-generated security test: {vuln[:60]}"""
    # TODO: craft a payload that exercises this path
    response = client.post("/endpoint", json={{"payload": "malicious_input"}})
    assert response.status_code != 500
    assert "error" not in response.json.get("data", "").lower()
'''
        cases.append({
            "id": tc_id,
            "title": vuln[:80],
            "category": "Security",
            "severity": "critical",
            "description": vuln,
            "stub": stub,
            "status": "pending",
        })

    # --- from requirement_status (Agent 3) ---
    for rs in assessment.get("requirement_status", []):
        if rs.get("status") in ("Failed", "Partial"):
            req_id = rs.get("req_id", "REQ-??")
            rationale = rs.get("rationale", "")
            tc_id = f"TC-AUDIT-{req_id}"
            stub = f'''\
def test_audit_{req_id.lower().replace("-", "_")}():
    """Regression test for auditor finding on {req_id}: {rationale[:60]}"""
    # TODO: implement the golden path fix and verify here
    result = invoke_system_under_test()
    assert result  # replace with real assertion
'''
            cases.append({
                "id": tc_id,
                "title": f"Regression: {req_id} ({rs.get('status')})",
                "category": "Requirements",
                "severity": "high",
                "description": rationale,
                "stub": stub,
                "status": "pending",
            })

    # --- tension-based integration tests (Agent 3) ---
    for i, tension in enumerate(assessment.get("tension_report", []), start=1):
        tc_id = f"TC-INT-{i:02d}"
        desc = tension.get("description", "")
        stub = f'''\
def test_integration_{i:02d}():
    """Integration test for tension: {desc[:60]}"""
    # Product impact : {tension.get("product_impact", "")}
    # Safety impact  : {tension.get("safety_impact", "")}
    result = invoke_system_under_test()
    assert result  # replace with real assertion
'''
        cases.append({
            "id": tc_id,
            "title": f"Integration: {desc[:70]}",
            "category": "Integration",
            "severity": "high",
            "description": desc,
            "stub": stub,
            "status": "pending",
        })

    return cases


# ---------------------------------------------------------------------------
# Real pipeline streaming
# ---------------------------------------------------------------------------

def _stream_real_pipeline(prd: str, tech_spec: str, source_code: str) -> Generator[str, None, None]:
    """Run the live three-agent pipeline and yield SSE events."""
    from code_sentinel.agents import ContextCartographer, LogicTranslator, AdversarialAuditor
    from code_sentinel.models import PipelineState

    state = PipelineState(prd=prd, technical_spec=tech_spec, source_code=source_code)

    steps = [
        ("ContextCartographer", "Extracting requirements & constraints", ContextCartographer()),
        ("LogicTranslator",     "Analysing control flow & data paths",   LogicTranslator()),
        ("AdversarialAuditor",  "Running adversarial evaluation",         AdversarialAuditor()),
    ]

    for i, (name, label, agent) in enumerate(steps, start=1):
        yield _sse({"event": "step_start", "agent": name, "step": i, "label": label})
        try:
            state = agent.run(state)
        except RuntimeError as exc:
            yield _sse({"event": "error", "message": str(exc)})
            return

        payload: dict = {}
        if name == "ContextCartographer" and state.truth_schema:
            payload = state.truth_schema.model_dump()
        elif name == "LogicTranslator" and state.logic_tree:
            payload = state.logic_tree.model_dump()
        elif name == "AdversarialAuditor" and state.assessment:
            payload = state.assessment.model_dump()

        yield _sse({"event": "step_done", "agent": name, "step": i, "payload": payload})

    # Derive test cases from all three outputs
    truth = state.truth_schema.model_dump() if state.truth_schema else {}
    tree  = state.logic_tree.model_dump()   if state.logic_tree   else {}
    audit = state.assessment.model_dump()   if state.assessment   else {}
    test_cases = _derive_test_cases(truth, tree, audit)
    yield _sse({"event": "test_cases", "test_cases": test_cases})
    yield _sse({"event": "done"})


# ---------------------------------------------------------------------------
# Mock pipeline streaming (no LLM required)
# ---------------------------------------------------------------------------

MOCK_TRUTH_SCHEMA = {
    "constraints": [
        {"id": "REQ-01", "type": "hard",  "description": "API response latency must be under 200ms at P95", "metric": "P95 < 200ms"},
        {"id": "REQ-02", "type": "hard",  "description": "All endpoints must enforce JWT authentication",   "metric": "401 on missing/invalid token"},
        {"id": "REQ-03", "type": "hard",  "description": "Passwords must be hashed with bcrypt, cost >= 12","metric": "bcrypt cost factor ≥ 12"},
        {"id": "REQ-04", "type": "hard",  "description": "Raw user input must never be written to DB without sanitisation", "metric": "0 unsanitised writes"},
        {"id": "REQ-05", "type": "soft",  "description": "Note content must be encrypted at rest",          "metric": "AES-256-GCM"},
    ],
    "product_goals": [
        {"id": "GOAL-01", "description": "Allow authenticated users to create, read, and delete notes", "priority": "P0"},
        {"id": "GOAL-02", "description": "Support 10,000 concurrent users",                            "priority": "P0"},
        {"id": "GOAL-03", "description": "Real-time collaboration is out of scope for v2",             "priority": "P2"},
    ],
    "tensions": [
        "REQ-01 (latency < 200ms) may conflict with REQ-05 (AES-256-GCM encryption on every read)"
    ],
    "missing_info": [
        "No specific SLA defined for write operations",
        "Backup / disaster recovery requirements not stated",
    ],
}

MOCK_LOGIC_TREE = {
    "primary_intent": "Provide a REST API for authenticated note CRUD operations backed by SQLite",
    "execution_path": [
        "Client sends credentials to POST /login",
        "Server computes MD5(password) and compares to stored hash",
        "On match, server signs a JWT with a hardcoded secret and returns it",
        "Client attaches Bearer token to GET /notes",
        "Server decodes token (no expiry check), interpolates user_id into raw SQL",
        "SQLite returns rows; server serialises to JSON",
    ],
    "entry_points": ["POST /login", "GET /notes"],
    "exit_points":  ["SQLite SELECT", "SQLite SELECT (auth check)", "JSON HTTP response"],
    "dependencies": [
        "flask — HTTP routing and request/response handling",
        "sqlite3 — persistence layer (stdlib, file-backed)",
        "hashlib — MD5 digest used for password hashing",
        "PyJWT  — JWT encode/decode",
    ],
    "data_vulnerabilities": [
        "SQL injection: username and password concatenated directly into query string in /login",
        "SQL injection: user_id from JWT payload interpolated into SQL in GET /notes — attacker controls token payload",
        "Weak hashing: MD5 used instead of bcrypt; trivially reversible with rainbow tables",
        "Hardcoded secret: JWT signing key is a string literal — leaked via source control",
        "No input length/type validation on any request field",
    ],
    "uncertainty_nodes": [],
}

MOCK_ASSESSMENT = {
    "compliance_score": 18,
    "hard_block": True,
    "requirement_status": [
        {"req_id": "REQ-01", "status": "Partial",  "rationale": "No latency instrumentation present; cannot verify P95 < 200ms"},
        {"req_id": "REQ-02", "status": "Partial",  "rationale": "JWT decode present but no expiry or audience check"},
        {"req_id": "REQ-03", "status": "Failed",   "rationale": "MD5 used instead of bcrypt; does not meet cost-factor requirement"},
        {"req_id": "REQ-04", "status": "Failed",   "rationale": "Raw f-string interpolation in both SQL queries — SQL injection confirmed"},
        {"req_id": "REQ-05", "status": "Failed",   "rationale": "No AES-256-GCM or any encryption applied to note bodies"},
    ],
    "tension_report": [
        {
            "description": "Speed optimisation via MD5 vs. security requirement for bcrypt",
            "product_impact": "MD5 is ~100× faster than bcrypt at cost 12",
            "safety_impact":  "MD5 hashes are trivially cracked; entire user credential store is at risk",
        },
        {
            "description": "Inline SQL construction vs. latency target",
            "product_impact": "Parameterised queries add negligible overhead (<1ms)",
            "safety_impact":  "Direct string interpolation enables full SQL injection — attacker can dump or destroy the database",
        },
    ],
    "golden_path": (
        "1. Replace MD5 with `bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12))`. "
        "2. Move JWT secret to `os.environ['JWT_SECRET']` and add `options={'verify_exp': True}` to decode. "
        "3. Replace all f-string SQL with parameterised queries: `cursor.execute('SELECT … WHERE username=?', (username,))`. "
        "4. Apply AES-256-GCM encryption to `note.body` before INSERT using `cryptography.hazmat.primitives.ciphers.aead.AESGCM`."
    ),
    "summary": (
        "The implementation contains multiple critical vulnerabilities: SQL injection on two endpoints, "
        "MD5 password hashing, and a hardcoded JWT secret. None of the hard security constraints from the PRD "
        "are satisfied. A HARD BLOCK is issued — this build must not be deployed."
    ),
}


def _stream_mock_pipeline(delay: float = 1.2) -> Generator[str, None, None]:
    """Yield the same deterministic SSE events as the real pipeline, with artificial delays."""
    steps = [
        ("ContextCartographer", "Extracting requirements & constraints", MOCK_TRUTH_SCHEMA),
        ("LogicTranslator",     "Analysing control flow & data paths",   MOCK_LOGIC_TREE),
        ("AdversarialAuditor",  "Running adversarial evaluation",         MOCK_ASSESSMENT),
    ]
    for i, (name, label, payload) in enumerate(steps, start=1):
        yield _sse({"event": "step_start", "agent": name, "step": i, "label": label})
        time.sleep(delay)
        yield _sse({"event": "step_done",  "agent": name, "step": i, "payload": payload})

    test_cases = _derive_test_cases(MOCK_TRUTH_SCHEMA, MOCK_LOGIC_TREE, MOCK_ASSESSMENT)
    yield _sse({"event": "test_cases", "test_cases": test_cases})
    yield _sse({"event": "done"})


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory("frontend", "index.html")


@app.route("/api/analyze", methods=["POST"])
def analyze():
    data        = request.get_json(force=True) or {}
    prd         = data.get("prd", "").strip()
    tech_spec   = data.get("technical_spec", "").strip()
    source_code = data.get("source_code", "").strip()

    if not (prd and source_code):
        return {"error": "prd and source_code are required"}, 400

    if not os.environ.get("OPENAI_API_KEY"):
        return {"error": "OPENAI_API_KEY is not configured on the server."}, 503

    return Response(
        _stream_real_pipeline(prd, tech_spec, source_code),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/analyze/mock", methods=["POST"])
def analyze_mock():
    return Response(
        _stream_mock_pipeline(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Code Sentinel UI → http://localhost:{port}")
    app.run(debug=True, port=port, threaded=True)
