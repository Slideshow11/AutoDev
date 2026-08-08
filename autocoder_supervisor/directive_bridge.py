"""Bridge between the orchestration relay and the supervisor's worker launch.

The relay v1 lives in ``autocoder_orchestration.review_repair_relay``.
The supervisor's worker launch lives in
``autocoder_supervisor.supervisor.launch_worker``. Both packages
are independent — the supervisor does not import the orchestration
code (it MUST stay deployable without the orchestration stack),
and the relay does not invoke the supervisor's subprocess layer.

This module is the THIN bridge that connects them. It reads the
canonical directive that the relay writes to the evidence root
and returns the relay-built worker prompt. The supervisor's
``launch_worker`` consults ``resolve_worker_prompt`` and uses
the directive prompt when one is available; otherwise it falls
back to the operator-supplied ``resume_prompt_template``.

The bridge is intentionally small — three functions, no state,
no subprocess. It is the smallest possible change that makes the
supervisor honour the relay's directive contract without
duplicating any of the supervisor's existing prompt / launch
mechanisms.

Deployment contract
-------------------

The bridge honours two environment variables:

- ``AED_DIRECTIVE_PATH`` — explicit path to the directive.json
  artifact (overrides the default location).
- ``AED_EVIDENCE_ROOT`` — the canonical evidence root; the
  bridge looks for ``<evidence_root>/directive.json`` when
  ``AED_DIRECTIVE_PATH`` is unset.

The default location is exactly the canonical artifact path
the relay writes to:

    <evidence_root>/directive.json

The supervisor's existing ``evidence_root`` configuration is
passed through unchanged. The bridge is a consumer, not a
writer.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional


# Direct import would create a cycle (orchestration does not
# depend on supervisor). The bridge reads the directive via the
# same canonical artifact writer that the relay uses, but it
# only needs the JSON payload, so a direct read is sufficient.
# The format contract is documented by the relay's
# ``RELAY_SCHEMA_VERSION`` constant.
_RELAY_SCHEMA_VERSION = "autocoder.review_repair_relay.v1"


def _resolve_directive_path(explicit: Optional[str] = None) -> Optional[Path]:
    """Locate the canonical directive artifact.

    Returns ``None`` when no directive is configured; the caller
    then falls back to the operator-supplied resume prompt
    template. The function is intentionally silent on missing
    files — the supervisor's main loop logs the absence
    separately so a missing directive is observable.
    """
    if explicit:
        p = Path(explicit)
        return p if p.is_file() else None
    env_path = os.environ.get("AED_DIRECTIVE_PATH")
    if env_path:
        p = Path(env_path)
        if p.is_file():
            return p
    evidence_root = os.environ.get("AED_EVIDENCE_ROOT")
    if evidence_root:
        candidate = Path(evidence_root) / "directive.json"
        if candidate.is_file():
            return candidate
    return None


def _render_directive_prompt(directive: dict) -> str:
    """Render the canonical worker prompt from a directive dict.

    The relay renders the prompt via
    ``autocoder_orchestration.review_repair_relay.build_worker_prompt``
    on a ``RoundDecision`` object. The supervisor does not have
    a ``RoundDecision`` object — it has the directive file
    written by the relay. The bridge formats the same template
    here so the supervisor can produce a stable prompt without
    reverse-importing the orchestration package.

    The format MUST stay byte-identical to the relay's
    ``build_worker_prompt`` output. The two implementations are
    coupled by ``tests/test_directive_bridge.py`` which compares
    them through a representative directive.
    """
    # Identical template to autocoder_orchestration.review_repair_relay.
    # Field names match the relay's ReviewDirective.to_dict() shape.
    required = (
        "schema_version", "directive_id", "round_index",
        "head_sha", "repo", "pr_number", "summary",
        "findings",
    )
    for field_name in required:
        if field_name not in directive:
            raise ValueError(
                f"directive is missing required field: {field_name!r}"
            )
    if directive["schema_version"] != _RELAY_SCHEMA_VERSION:
        raise ValueError(
            f"directive schema_version {directive['schema_version']!r} "
            f"is not {_RELAY_SCHEMA_VERSION!r}"
        )
    # Deterministic SHA-256 (matches the relay's
    # ``ReviewDirective.compute_sha256`` which serializes the
    # full directive payload).
    import hashlib
    canonical = json.dumps(
        directive, sort_keys=True, separators=(",", ":"),
    )
    directive_sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    payload = json.dumps(directive, indent=2, sort_keys=True)
    return (
        f"[AED-AUTOCODER REPAIR DIRECTIVE — round {directive['round_index']}] "
        f"You are operating in the autonomous review/repair relay (v1) for "
        f"PR {directive['pr_number']} ({directive['repo']}).\n\n"
        f"Authoritative head: {directive['head_sha']}\n"
        f"Directive ID: {directive['directive_id']}\n"
        f"Directive SHA-256: {directive_sha}\n\n"
        "The relay has already collected exact-head CI and CodeRabbit evidence. "
        "Your job is to apply every P1 finding and the required CI failures. "
        "The directive below is the authoritative spec for this round — do not "
        "re-query the review surface; the relay has already done so.\n\n"
        f"Directive summary: {directive['summary']}\n\n"
        "```json\n"
        f"{payload}\n"
        "```\n\n"
        "Apply every P1 finding. Verify the repair by running the focused test "
        "suite and the CI gate. Commit and push. Do NOT amend history. Do NOT "
        "force-push. Do NOT merge. The relay will detect the new head "
        "automatically and run the next round or transition to qualification.\n\n"
        "Standing authorization is already recorded in run_state.json. Stop only "
        "when the relay signals 'enter_qualifying_readiness' or 'escalate_to_human' "
        "via the next round decision."
    )


def resolve_worker_prompt(
    *,
    directive_path: Optional[str] = None,
) -> Optional[str]:
    """Return the relay's worker prompt when a directive is on disk.

    Returns ``None`` when no directive is configured, when the
    configured file is missing, or when the file is malformed.
    The supervisor's main loop logs the configuration state so
    the absence is observable.

    The function NEVER invokes the relay's
    ``build_worker_prompt`` directly to avoid a package cycle.
    The two prompt-rendering paths are byte-identical for
    equivalent inputs (verified by
    ``tests/test_directive_bridge.py``).
    """
    path = _resolve_directive_path(directive_path)
    if path is None:
        return None
    try:
        text = path.read_text()
        directive = json.loads(text)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(directive, dict):
        return None
    try:
        return _render_directive_prompt(directive)
    except (ValueError, KeyError, TypeError):
        return None


__all__ = [
    "resolve_worker_prompt",
    "_render_directive_prompt",
]
