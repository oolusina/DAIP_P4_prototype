"""
Example entry point for The Code Sentinel pipeline.

Usage
-----
    export OPENAI_API_KEY=sk-...
    python main.py

The script ships with a self-contained example (a minimal Flask API with a
deliberately insecure endpoint) so you can observe the full pipeline output
without supplying real documents.
"""

from __future__ import annotations

import json
import logging
import sys

from dotenv import load_dotenv

from code_sentinel import CodeSentinelPipeline, PipelineState

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(message)s")

# ---------------------------------------------------------------------------
# Example inputs — swap these for real documents in production
# ---------------------------------------------------------------------------

EXAMPLE_PRD = """\
Product: SecureNotes API v2

Goals:
- Allow authenticated users to create, read, and delete personal notes.
- All note content must be encrypted at rest.
- API response latency must be under 200ms at P95.
- The system must support 10,000 concurrent users.

Non-Goals:
- Real-time collaboration features are out of scope for v2.

Security:
- All endpoints must enforce JWT authentication.
- Passwords must be hashed with bcrypt (cost factor >= 12).
- Raw user input must never be written to the database without sanitization.
"""

EXAMPLE_TECHNICAL_SPEC = """\
Stack: Python 3.11, Flask 3.x, PostgreSQL 15, Redis 7 (session cache).
Deployment: Docker on AWS ECS Fargate, behind an ALB with TLS 1.3.
Auth: JWT (HS256) with 15-minute access tokens; refresh tokens stored in Redis.
Encryption: AES-256-GCM applied to note body before INSERT.
Rate limiting: 100 req/min per user enforced at ALB layer.
"""

EXAMPLE_SOURCE_CODE = """\
from flask import Flask, request, jsonify
import sqlite3, hashlib, jwt, os

app = Flask(__name__)
SECRET = "hardcoded-secret-key"  # TODO: move to env var

def get_db():
    return sqlite3.connect("notes.db")

@app.route("/login", methods=["POST"])
def login():
    data = request.get_json()
    username = data["username"]
    password = data["password"]
    # MD5 hash — fast but weak
    pw_hash = hashlib.md5(password.encode()).hexdigest()
    conn = get_db()
    row = conn.execute(
        f"SELECT id FROM users WHERE username='{username}' AND pw_hash='{pw_hash}'"
    ).fetchone()
    if row:
        token = jwt.encode({"user_id": row[0]}, SECRET, algorithm="HS256")
        return jsonify({"token": token})
    return jsonify({"error": "Unauthorized"}), 401

@app.route("/notes", methods=["GET"])
def get_notes():
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    try:
        payload = jwt.decode(token, SECRET, algorithms=["HS256"])
    except Exception:
        return jsonify({"error": "Invalid token"}), 401
    conn = get_db()
    rows = conn.execute(
        f"SELECT id, body FROM notes WHERE user_id={payload['user_id']}"
    ).fetchall()
    return jsonify([{"id": r[0], "body": r[1]} for r in rows])

if __name__ == "__main__":
    app.run(debug=True)
"""


# ---------------------------------------------------------------------------
# Human-in-the-loop step hook
# ---------------------------------------------------------------------------

def on_step_complete(agent_name: str, state: PipelineState) -> None:
    """Pretty-print a summary after each agent completes."""
    print(f"\n{'='*60}")
    print(f"  {agent_name} — COMPLETE")
    print(f"{'='*60}")

    if agent_name == "ContextCartographer" and state.truth_schema:
        schema = state.truth_schema
        print(f"  Constraints : {len(schema.constraints)}")
        print(f"  Goals       : {len(schema.product_goals)}")
        print(f"  Tensions    : {len(schema.tensions)}")
        print(f"  Missing info: {len(schema.missing_info)}")

    elif agent_name == "LogicTranslator" and state.logic_tree:
        tree = state.logic_tree
        print(f"  Primary intent     : {tree.primary_intent}")
        print(f"  Execution steps    : {len(tree.execution_path)}")
        print(f"  Vulnerabilities    : {len(tree.data_vulnerabilities)}")
        print(f"  Uncertainty nodes  : {len(tree.uncertainty_nodes)}")

    elif agent_name == "AdversarialAuditor" and state.assessment:
        a = state.assessment
        print(f"  Compliance score   : {a.compliance_score}/100")
        print(f"  Hard block         : {'YES — DEPLOYMENT BLOCKED' if a.hard_block else 'No'}")
        print(f"  Requirement checks : {len(a.requirement_status)}")
        if a.summary:
            print(f"\n  Summary:\n  {a.summary}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    pipeline = CodeSentinelPipeline(on_step_complete=on_step_complete)

    print("\nThe Code Sentinel — starting pipeline…\n")
    try:
        state = pipeline.run(
            prd=EXAMPLE_PRD,
            technical_spec=EXAMPLE_TECHNICAL_SPEC,
            source_code=EXAMPLE_SOURCE_CODE,
        )
    except RuntimeError as exc:
        print(f"\n[FATAL] Pipeline aborted:\n{exc}", file=sys.stderr)
        sys.exit(1)

    # Dump the full assessment as formatted JSON
    if state.assessment:
        print("\n" + "="*60)
        print("  FINAL ASSESSMENT (JSON)")
        print("="*60)
        print(state.assessment.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
