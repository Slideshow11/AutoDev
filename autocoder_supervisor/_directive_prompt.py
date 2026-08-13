"""Single source of truth for the relay-directive worker prompt.

The relay (in ``autocoder_orchestration.review_repair_relay``)
and the supervisor's directive bridge (in
``autocoder_supervisor.directive_bridge``) both render the
canonical worker prompt from a directive dict. The two
implementations MUST stay byte-identical so the supervisor
can render the same prompt the relay produced.

This module exists at the supervisor layer because the
supervisor must not import the orchestration package (the
orchestration code is dynamically optional). The relay
mirrors this template through its own ``WORKER_PROMPT_TEMPLATE``
constant; the two are paired by the
``tests/test_directive_bridge.py`` companion test.

Template placeholders:
- ``{round_index}`` — ``int``
- ``{pr_number}`` — ``int``
- ``{repo}`` — ``str``
- ``{head_sha}`` — ``str`` (40 or 64 lowercase hex)
- ``{directive_id}`` — ``str``
- ``{directive_sha256}`` — ``str`` (64 lowercase hex)
- ``{summary}`` — ``str``
- ``{directive_json}`` — ``str`` (pretty-printed JSON)
- ``{result_contract_id}`` — ``str`` (Round-54/C22 §2 result
   contract id. ``NONE`` when not provided for the
   backward-compatible path; explicit value is required by
   the production supervisor pipeline.)

The template is intentionally a single string constant. The
two implementations are paired by the byte-identical-output
test, so any drift is caught immediately.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional


# The canonical template. Both the relay and the bridge render
# this template with the same substitution dict. The
# ``tests/test_directive_bridge.py`` companion test verifies
# byte-identical output across the two implementations.
DIRECTIVE_PROMPT_TEMPLATE = (
    "[AED-AUTOCODER REPAIR DIRECTIVE — round {round_index}] "
    "You are operating in the autonomous review/repair relay (v1) for "
    "PR {pr_number} ({repo}).\n\n"
    "Authoritative head: {head_sha}\n"
    "Directive ID: {directive_id}\n"
    "Directive SHA-256: {directive_sha256}\n"
    "Result contract id: {result_contract_id}\n\n"
    "The relay has already collected exact-head CI and CodeRabbit evidence. "
    "Your job is to apply every P1 finding and the required CI failures. "
    "P2 findings are preferred but not blocking — apply them when "
    "straightforward (clean fix, no architectural change). The directive "
    "below is the authoritative spec for this round — do not "
    "re-query the review surface; the relay has already done so.\n\n"
    "Directive summary: {summary}\n\n"
    "```json\n"
    "{directive_json}\n"
    "```\n\n"
    "Apply every P1 finding. Apply each P2 finding if the fix is "
    "straightforward (single-line, obvious, no behavior change). Verify "
    "the repair by running the focused test "
    "suite and the CI gate. Commit and push. Do NOT amend history. Do NOT "
    "force-push. Do NOT merge. The relay will detect the new head "
    "automatically and run the next round or transition to qualification.\n\n"
    "Standing authorization is already recorded in run_state.json. Stop only "
    "when the relay signals 'enter_qualifying_readiness' or 'escalate_to_human' "
    "via the next round decision.\n\n"
    "ROUND-39 NO-OP CONTRACT (Section 4):\n"
    "Before creating any commit you MUST classify every finding in this directive "
    "as one of:\n"
    "  A. REAL_REPAIR_REQUIRED  - the defect is genuinely present at the current "
    "     exact head and a source edit is required to repair it.\n"
    "  B. ALREADY_SATISFIED    - the defect is genuinely present at the current "
    "     exact head but has already been fixed by an earlier commit on this "
    "     branch (or by a prior round's worker). Inspect the current "
    "     exact-head code; if the relevant code path already implements the "
    "     required behavior, this finding is ALREADY_SATISFIED.\n"
    "  C. SUPERSEDED          - the finding references a path/line/comment that "
    "     no longer exists at the current exact head or that has been "
    "     superseded by a later change. The finding cannot be repaired "
    "     because its target is gone.\n"
    "  D. INSUFFICIENT_EVIDENCE - you cannot determine the disposition with "
    "     the available evidence. Do not fabricate a fix.\n"
    "You MUST answer explicitly, per finding:\n"
    "  WHAT CURRENT DEFECT DOES THIS DIFF REPAIR?\n"
    "If the answer is none (categories B / C / D for every finding), you MUST "
    "NOT create a commit, you MUST NOT push, you MUST NOT modify the repository. "
    "Instead, write a durable worker attempt result that records the disposition "
    "of each finding, exit without changes, and let the supervisor reconcile.\n"
    "Successful work does not require creating a Git commit. The defining "
    "round-39 success signal is that AutoDev stops committing when there is "
    "nothing left to commit. A 'verify all P1 findings are still intact' "
    "commit with no new source edit is NOT convergence and IS a regression: "
    "it changes the head, invalidates exact-head evidence, resets the quiet "
    "window, can trigger provider auto-pause, and creates infinite churn.\n\n"
    "============================================================\n"
    "ROUND-54/C22 RESULT CONTRACT — REQUIRED FOR VALID ENVELOPE\n"
    "============================================================\n"
    "The exact prelaunch result contract id is:\n\n"
    "    {result_contract_id}\n\n"
    "Your final worker envelope MUST contain a field named exactly\n"
    "``result_contract_id`` whose value is the EXACT string above.\n"
    "If you omit the field, or echo a different value, the wrapper\n"
    "will fail the attempt closed (``WORKER_RESULT_INVALID``) and the\n"
    "head movement will be classified as UNATTRIBUTED_HEAD_ADVANCE —\n"
    "even if the commit was successfully pushed. The contract is the\n"
    "trust boundary between the supervisor and the worker; it is NOT\n"
    "optional.\n\n"
    "Required final envelope schema (camel/snake as shown; do not\n"
    "rename fields):\n\n"
    "===WORKER_RESULT_ENVELOPE===\n"
    "{{\n"
    '  "schema_version": "autocoder.worker_envelope.v1",\n'
    '  "attempt_id": "att-<TIMESTAMP>-<PID>",\n'
    '  "claim_id": "att-<TIMESTAMP>-<PID>",\n'
    '  "directive_digest": "<sha256 hex>",\n'
    '  "directive_id": "<uuid>",\n'
    '  "result_type": "NO_CHANGES_REQUIRED | REPAIR_PUSHED | REPAIR_COMMIT_PRODUCED | COMMIT_PRODUCED_NOT_PUSHED | WORKER_EXECUTION_FAILED",\n'
    '  "produced_commit_shas": ["<sha>", ...] | [],\n'
    '  "pushed_commit_shas": ["<sha>", ...] | [],\n'
    '  "completed_at": "<ISO-8601 UTC>",\n'
    '  "prelaunch_head": "<40-char hex>",\n'
    '  "result_contract_id": "{result_contract_id}",\n'
    '  "attempt_nonce": "att-<TIMESTAMP>",\n'
    '  "no_changes_required_proof": {{\n'
    '    "findings": [\n'
    '      {{"finding_id": "thread:...", "disposition": "ALREADY_SATISFIED|REPAIRED|SUPERSEDED|STILL_ACTIONABLE|INCOMPLETE_EVIDENCE"}},\n'
    '      ...\n'
    '    ],\n'
    '    "source": "round50_envelope_parser"\n'
    '  }}\n'
    "}}\n"
    "===END_ENVELOPE===\n\n"
    "The wrapper captures your stdout, parses the envelope, and\n"
    "writes the canonical WorkerResultArtifact to disk. The\n"
    "supervisor-side validator cross-checks:\n"
    "  expected_result_contract_id == wrapper CLI --result-contract-id\n"
    "  observed_result_contract_id == envelope.result_contract_id\n"
    "MUST be equal, or the attempt is invalid.\n"
)


def compute_directive_sha256(directive: dict) -> str:
    """SHA-256 of the canonical serialization of the directive.

    The canonical form is the JSON serialization of the
    directive payload with sorted keys and compact separators,
    excluding the persisted ``_sha256`` field itself. This
    matches the relay's ``ReviewDirective.compute_sha256``.
    """
    canonical_fields = {k: v for k, v in directive.items() if k != "_sha256"}
    canonical = json.dumps(
        canonical_fields, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def render_directive_prompt(
    directive: dict,
    *,
    result_contract_id: Optional[str] = None,
) -> str:
    """Render the canonical worker prompt from a directive dict.

    The caller is responsible for validating the directive
    shape (``_load_directive_payload`` in ``directive_bridge``
    performs the validation). This function only formats.

    The directive's persisted ``_sha256`` metadata is stripped
    from the embedded JSON so the bridge prompt is byte-identical
    to the relay's ``build_worker_prompt`` output (the relay
    serializes from ``to_dict()`` which does not include
    ``_sha256``).

    Round-54/C22 §2: ``result_contract_id`` is the trust-boundary
    value the worker MUST echo verbatim in its final envelope. The
    supervisor-generated ``result_contract_id`` is passed in
    here so the directive body and the contract spec section both
    contain the exact same string; the worker has no excuse to
    miss it. When omitted for backward compatibility with the
    relay's pure-directive render, the placeholder shows ``NONE``,
    but production callers MUST supply a real id.
    """
    # Strip the persisted _sha256 from the embedded JSON so the
    # bridge prompt matches the relay's to_dict() shape.
    payload_no_digest = {
        k: v for k, v in directive.items() if k != "_sha256"
    }
    payload = json.dumps(payload_no_digest, indent=2, sort_keys=True)
    return DIRECTIVE_PROMPT_TEMPLATE.format(
        round_index=directive["round_index"],
        pr_number=directive["pr_number"],
        repo=directive["repo"],
        head_sha=directive["head_sha"],
        directive_id=directive["directive_id"],
        directive_sha256=compute_directive_sha256(directive),
        result_contract_id=result_contract_id if result_contract_id else "NONE",
        summary=directive["summary"],
        directive_json=payload,
    )


__all__ = [
    "DIRECTIVE_PROMPT_TEMPLATE",
    "compute_directive_sha256",
    "render_directive_prompt",
]
