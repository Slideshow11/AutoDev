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

The template is intentionally a single string constant. The
two implementations are paired by the byte-identical-output
test, so any drift is caught immediately.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


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
    "Directive SHA-256: {directive_sha256}\n\n"
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
    "window, can trigger provider auto-pause, and creates infinite churn.\n"
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


def render_directive_prompt(directive: dict) -> str:
    """Render the canonical worker prompt from a directive dict.

    The caller is responsible for validating the directive
    shape (``_load_directive_payload`` in ``directive_bridge``
    performs the validation). This function only formats.

    The directive's persisted ``_sha256`` metadata is stripped
    from the embedded JSON so the bridge prompt is byte-identical
    to the relay's ``build_worker_prompt`` output (the relay
    serializes from ``to_dict()`` which does not include
    ``_sha256``).
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
        summary=directive["summary"],
        directive_json=payload,
    )


__all__ = [
    "DIRECTIVE_PROMPT_TEMPLATE",
    "compute_directive_sha256",
    "render_directive_prompt",
]
