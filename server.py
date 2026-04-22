"""
Flask server for The Code Sentinel demo UI.

Endpoints
---------
POST /api/analyze                   Run the real pipeline (requires OPENAI_API_KEY).
POST /api/analyze/mock              Return deterministic fake data — no LLM needed.
PATCH /api/tests/<tc_id>/status     Update a test case's workflow status.
POST  /api/tests/<tc_id>/comments   Add an engineer comment to a test case.
PATCH /api/tests/<tc_id>/code       Save an edited stub back to the server.
GET   /                             Serve the single-page frontend.

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
import uuid
from datetime import datetime, timezone
from typing import Generator

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, send_from_directory

load_dotenv()

app = Flask(__name__, static_folder="frontend", static_url_path="")

# ---------------------------------------------------------------------------
# In-memory test-case store  (resets on server restart — fine for a demo)
# ---------------------------------------------------------------------------
_test_store: dict[str, dict] = {}   # tc_id → test-case dict


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Test-case derivation
# ---------------------------------------------------------------------------

def _derive_test_cases(
    truth_schema: dict,
    logic_tree: dict,
    assessment: dict,
) -> list[dict]:
    """
    Convert pipeline outputs into interactive test-case objects.

    Each test case carries:
      id                  — stable identifier
      title               — human-readable name
      category            — internal only, not surfaced in the UI
      severity            — critical | high | medium | low
      description         — what to verify
      stub                — auto-generated pytest stub
      code_recommendation — concrete, copy-paste-ready code fix from the Auditor
      acceptance_criteria — bullet list of what "passing" looks like
      linked_req          — requirement IDs this test is tied to
      status              — pending | in_progress | passed | failed | blocked
      assignee            — null (engineer can claim it)
      comments            — []
      history             — [{ ts, field, old, new }]
    """
    cases: list[dict] = []

    # ── Agent 1: constraints ────────────────────────────────────────────────
    for c in truth_schema.get("constraints", []):
        req_id   = c.get("id", "REQ-??")
        metric   = c.get("metric", "")
        desc     = c.get("description", "")
        severity = "critical" if c.get("type") == "hard" else "medium"
        is_perf  = any(k in desc.lower() for k in ("latency", "ms", "throughput", "memory", "cpu", "concurrent"))
        category = "Performance" if is_perf else "Requirements"

        stub = (
            f'def test_{req_id.lower().replace("-","_")}():\n'
            f'    """Auto-generated from {req_id}: {desc}"""\n'
            f'    # TODO: instrument your system and assert the metric below\n'
            f'    # Expected: {metric or desc}\n'
            f'    result = invoke_system_under_test()\n'
            f'    assert result  # replace with real assertion\n'
        )

        if is_perf:
            rec = (
                f"# Instrument with a timing decorator or middleware:\n"
                f"import time\n"
                f"start = time.perf_counter()\n"
                f"response = client.get('/endpoint')\n"
                f"elapsed_ms = (time.perf_counter() - start) * 1000\n"
                f"assert elapsed_ms < 200, f'P95 latency {{elapsed_ms:.1f}}ms exceeds 200ms SLA'"
            )
            criteria = [
                f"P95 response time is below 200 ms under normal load",
                f"Test is run against a staging environment with production-equivalent data volume",
                f"Load generator produces at least 100 concurrent requests",
            ]
        else:
            rec = (
                f"# Verify the requirement is enforced at the boundary:\n"
                f"response = client.post('/endpoint', json={{...}})\n"
                f"assert response.status_code == 200\n"
                f"# assert specific field or behaviour: {metric or desc}"
            )
            criteria = [
                f"The system satisfies: {desc}",
                f"Edge cases (empty input, boundary values) are covered",
                f"Test is deterministic and repeatable in CI",
            ]

        cases.append(_make_case(
            tc_id=f"TC-{req_id}",
            title=desc[:80],
            category=category,
            severity=severity,
            description=f"{req_id} — {desc}" + (f" ({metric})" if metric else ""),
            stub=stub,
            code_recommendation=rec,
            acceptance_criteria=criteria,
            linked_reqs=[req_id],
        ))

    # ── Agent 2: data vulnerabilities ──────────────────────────────────────
    _VULN_FIXES = {
        "sql injection": (
            "# Use parameterised queries — never interpolate user data into SQL\n"
            "cursor.execute(\n"
            "    'SELECT id FROM users WHERE username = ? AND pw_hash = ?',\n"
            "    (username, pw_hash),\n"
            ")\n"
            "# Also validate input types before the query:\n"
            "if not isinstance(username, str) or len(username) > 64:\n"
            "    return jsonify({'error': 'Invalid input'}), 400"
        ),
        "md5": (
            "# Replace MD5 with bcrypt (cost factor ≥ 12):\n"
            "import bcrypt\n"
            "# On registration:\n"
            "pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12))\n"
            "# On login:\n"
            "if not bcrypt.checkpw(password.encode(), stored_hash):\n"
            "    return jsonify({'error': 'Unauthorized'}), 401"
        ),
        "weak hash": (
            "import bcrypt\n"
            "pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12))\n"
            "assert bcrypt.checkpw(password.encode(), pw_hash)"
        ),
        "hardcoded secret": (
            "# Move JWT secret to environment variable — never commit secrets:\n"
            "import os\n"
            "JWT_SECRET = os.environ['JWT_SECRET']  # set in .env / secrets manager\n"
            "# Rotate old tokens after changing the secret.\n"
            "# Use RS256 (asymmetric) for production instead of HS256."
        ),
        "validation": (
            "from flask import abort\n"
            "def validate_request(data: dict, required: list[str]) -> None:\n"
            "    for key in required:\n"
            "        if key not in data or not isinstance(data[key], str):\n"
            "            abort(400, description=f'Missing or invalid field: {key}')\n"
            "        if len(data[key]) > 256:\n"
            "            abort(400, description=f'Field too long: {key}')"
        ),
    }

    for i, vuln in enumerate(logic_tree.get("data_vulnerabilities", []), start=1):
        tc_id = f"TC-SEC-{i:02d}"
        vuln_lower = vuln.lower()
        rec = next(
            (fix for keyword, fix in _VULN_FIXES.items() if keyword in vuln_lower),
            (
                f"# Address the identified vulnerability:\n"
                f"# {vuln}\n"
                f"# Apply input sanitisation, use safe APIs, and add regression tests."
            ),
        )
        criteria = [
            "Vulnerability is not exploitable with the fix applied",
            "Automated fuzzing / DAST scan shows no injection points",
            "Code review sign-off from a second engineer",
            "OWASP Top-10 checklist item marked resolved",
        ]
        stub = (
            f'def test_security_{i:02d}():\n'
            f'    """Auto-generated security test: {vuln[:60]}"""\n'
            f'    # TODO: craft a payload that exercises this path\n'
            f'    response = client.post("/endpoint", json={{"payload": "<malicious>"}})\n'
            f'    assert response.status_code in (400, 401, 403), "Should reject malicious input"\n'
            f'    # Ensure no stack trace leaks in body\n'
            f'    assert "Traceback" not in response.get_data(as_text=True)\n'
        )
        cases.append(_make_case(
            tc_id=tc_id,
            title=vuln[:80],
            category="Security",
            severity="critical",
            description=vuln,
            stub=stub,
            code_recommendation=rec,
            acceptance_criteria=criteria,
            linked_reqs=[],
        ))

    # ── Agent 3: failed / partial requirements ──────────────────────────────
    golden = assessment.get("golden_path", "")
    for rs in assessment.get("requirement_status", []):
        if rs.get("status") not in ("Failed", "Partial"):
            continue
        req_id    = rs.get("req_id", "REQ-??")
        rationale = rs.get("rationale", "")
        tc_id     = f"TC-AUDIT-{req_id}"
        rec = (
            f"# Golden path recommendation from the Adversarial Auditor:\n"
            + "\n".join(f"# {line}" for line in golden.splitlines())
            if golden else
            f"# TODO: implement the golden path fix for {req_id}\n# Rationale: {rationale}"
        )
        criteria = [
            f"Requirement {req_id} transitions from '{rs['status']}' to 'Met'",
            "Regression test added to CI pipeline",
            "Auditor re-run scores compliance ≥ 80 after fix",
        ]
        stub = (
            f'def test_audit_{req_id.lower().replace("-","_")}():\n'
            f'    """Regression: {req_id} — {rationale[:60]}"""\n'
            f'    # Implement the golden path fix, then assert the requirement is met\n'
            f'    result = invoke_system_under_test()\n'
            f'    assert result  # replace with concrete assertion\n'
        )
        cases.append(_make_case(
            tc_id=tc_id,
            title=f"Regression: {req_id} ({rs.get('status')})",
            category="Requirements",
            severity="high",
            description=rationale,
            stub=stub,
            code_recommendation=rec,
            acceptance_criteria=criteria,
            linked_reqs=[req_id],
        ))

    # ── Agent 3: tension-based integration tests ────────────────────────────
    for i, tension in enumerate(assessment.get("tension_report", []), start=1):
        tc_id    = f"TC-INT-{i:02d}"
        desc     = tension.get("description", "")
        p_impact = tension.get("product_impact", "")
        s_impact = tension.get("safety_impact", "")
        rec = (
            f"# Resolve the product/safety tension:\n"
            f"# Product concern : {p_impact}\n"
            f"# Safety concern  : {s_impact}\n"
            f"#\n"
            f"# Strategy: add a feature flag or tiered enforcement so the\n"
            f"# safe path is the default and the fast path requires explicit opt-in."
        )
        criteria = [
            f"Integration test covers both the happy path and the adversarial path",
            f"Safety constraint is not bypassed when performance target is met",
            f"Load test confirms no regression in P95 after applying the safe fix",
        ]
        stub = (
            f'def test_integration_{i:02d}():\n'
            f'    """Integration: {desc[:60]}"""\n'
            f'    # Happy path: product functionality still works\n'
            f'    result = invoke_system_under_test(happy_path=True)\n'
            f'    assert result.ok\n'
            f'    # Safety path: adversarial input is rejected\n'
            f'    result = invoke_system_under_test(adversarial=True)\n'
            f'    assert not result.ok or result.is_safe\n'
        )
        cases.append(_make_case(
            tc_id=tc_id,
            title=f"Integration: {desc[:70]}",
            category="Integration",
            severity="high",
            description=desc,
            stub=stub,
            code_recommendation=rec,
            acceptance_criteria=criteria,
            linked_reqs=[],
        ))

    # Persist to store so the interaction endpoints can mutate them
    for tc in cases:
        _test_store[tc["id"]] = tc

    return cases


def _make_case(
    *,
    tc_id: str,
    title: str,
    category: str,
    severity: str,
    description: str,
    stub: str,
    code_recommendation: str,
    acceptance_criteria: list[str],
    linked_reqs: list[str],
) -> dict:
    return {
        "id":                   tc_id,
        "title":                title,
        "category":             category,
        "severity":             severity,
        "description":          description,
        "stub":                 stub,
        "code_recommendation":  code_recommendation,
        "acceptance_criteria":  acceptance_criteria,
        "linked_reqs":          linked_reqs,
        "status":               "pending",
        "assignee":             None,
        "comments":             [],
        "history":              [],
        "created_at":           _now(),
        "updated_at":           _now(),
    }


# ---------------------------------------------------------------------------
# Real pipeline streaming
# ---------------------------------------------------------------------------

def _stream_real_pipeline(prd: str, tech_spec: str, source_code: str) -> Generator[str, None, None]:
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

    truth      = state.truth_schema.model_dump() if state.truth_schema else {}
    tree       = state.logic_tree.model_dump()   if state.logic_tree   else {}
    audit      = state.assessment.model_dump()   if state.assessment   else {}
    test_cases = _derive_test_cases(truth, tree, audit)
    yield _sse({"event": "test_cases", "test_cases": test_cases})
    yield _sse({"event": "done"})


# ---------------------------------------------------------------------------
# Mock pipeline streaming
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
    "tensions": ["REQ-01 (latency < 200ms) may conflict with REQ-05 (AES-256-GCM encryption on every read)"],
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
# Routes — pipeline
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


# ---------------------------------------------------------------------------
# Routes — test-case interaction (used by the QA / engineering team UI)
# ---------------------------------------------------------------------------

@app.route("/api/tests/<tc_id>/status", methods=["PATCH"])
def update_status(tc_id: str):
    """Set workflow status: pending | in_progress | passed | failed | blocked"""
    tc = _test_store.get(tc_id)
    if not tc:
        return {"error": "Test case not found"}, 404
    body   = request.get_json(force=True) or {}
    new_st = body.get("status", "").strip()
    valid  = {"pending", "in_progress", "passed", "failed", "blocked"}
    if new_st not in valid:
        return {"error": f"status must be one of {sorted(valid)}"}, 400

    old_st = tc["status"]
    tc["status"]     = new_st
    tc["updated_at"] = _now()
    if body.get("assignee") is not None:
        tc["assignee"] = body["assignee"]
    tc["history"].append({"ts": _now(), "field": "status", "old": old_st, "new": new_st})
    return jsonify(tc)


@app.route("/api/tests/<tc_id>/comments", methods=["POST"])
def add_comment(tc_id: str):
    """Append a threaded comment from an engineer."""
    tc = _test_store.get(tc_id)
    if not tc:
        return {"error": "Test case not found"}, 404
    body    = request.get_json(force=True) or {}
    text    = body.get("text", "").strip()
    author  = body.get("author", "Engineer").strip()
    if not text:
        return {"error": "text is required"}, 400

    comment = {
        "id":     str(uuid.uuid4())[:8],
        "author": author,
        "text":   text,
        "ts":     _now(),
        "edited": False,
    }
    tc["comments"].append(comment)
    tc["updated_at"] = _now()
    return jsonify(comment), 201


@app.route("/api/tests/<tc_id>/code", methods=["PATCH"])
def update_code(tc_id: str):
    """Save an edited stub or code recommendation back to the server."""
    tc = _test_store.get(tc_id)
    if not tc:
        return {"error": "Test case not found"}, 404
    body  = request.get_json(force=True) or {}
    field = body.get("field", "stub")          # 'stub' | 'code_recommendation'
    code  = body.get("code", "")
    if field not in ("stub", "code_recommendation"):
        return {"error": "field must be 'stub' or 'code_recommendation'"}, 400

    old = tc.get(field, "")
    tc[field]        = code
    tc["updated_at"] = _now()
    tc["history"].append({"ts": _now(), "field": field, "old": old[:120] + ("…" if len(old) > 120 else ""), "new": "[updated]"})
    return jsonify({"ok": True, "field": field, "updated_at": tc["updated_at"]})


# ---------------------------------------------------------------------------
# Dev server entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Code Sentinel UI → http://localhost:{port}")
    app.run(debug=True, port=port, threaded=True)
