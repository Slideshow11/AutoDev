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

Expected head
--------------

The supervisor's ``launch_worker`` passes
``AUTHORITATIVE_HEAD`` as ``expected_head`` to
``resolve_worker_prompt``. The bridge rejects directives whose
``head_sha`` does not match; this is the exact-head guard from
invariant I-08 ("review evidence is bound to the exact current
head"). A head mismatch is logged and the bridge returns
``None`` so the supervisor falls back to the operator-supplied
resume prompt.

Failure surface
---------------

The bridge surfaces directive failures through a structured
``DirectiveLoadFailure`` exception so the supervisor can log
the failure mode (missing / unreadable / malformed JSON /
non-dict / schema-invalid / digest mismatch / head mismatch /
wrong schema version) instead of silently retrying. The
exception is caught at the supervisor's consultation site; the
fallback path is preserved byte-for-byte.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ._directive_prompt import render_directive_prompt


# The relay's schema version. The bridge refuses any other
# version so a stale directive from a previous harness cannot
# drive the supervisor.
_RELAY_SCHEMA_VERSION = "autocoder.review_repair_relay.v1"


# Same escalation keywords as the relay's build_directive.
# The bridge rejects any directive whose body contains
# these so a hand-edited directive cannot bypass the
# human-only authority via the directive feed. The
# operator MUST direct the relay through the
# review-repair-round CLI; the bridge is a consumer of
# validated directives only.
_ESCALATION_KEYWORDS = frozenset({
    "force push",
    "rewrite history",
    "delete branch",
    "disable tests",
    "skip ci",
    "merge pr",
    "close pr",
    "bypass guard",
    "ignore gate",
})


class DirectiveLoadFailure(Exception):
    """Raised when a directive cannot be loaded or accepted.

    The supervisor catches this exception and falls back to
    the operator-supplied resume prompt. The ``reason`` field
    is logged so the operator can see the structured failure
    mode.
    """

    def __init__(self, reason: str, path: Optional[Path] = None) -> None:
        self.reason = reason
        self.path = path
        super().__init__(f"{reason} ({path})" if path else reason)


@dataclass(frozen=True)
class ResolvedDirective:
    """The result of a successful directive lookup.

    The supervisor consults ``resolved.prompt`` for the worker
    command and ``resolved.path`` for the log line. The
    ``directive_sha256`` is the canonical digest the worker
    will see in its prompt.
    """

    path: Path
    prompt: str
    directive_sha256: str


def _resolve_directive_path(explicit: Optional[str] = None) -> Optional[Path]:
    """Locate the canonical directive artifact.

    Returns ``None`` when no directive is configured; the caller
    then falls back to the operator-supplied resume prompt
    template.

    Lookup order is operator-supplied explicit path, then
    ``AED_DIRECTIVE_PATH``, then ``AED_EVIDENCE_ROOT/directive.json``.
    Each candidate is checked for ``is_file()`` so a missing
    file is silently treated as "no directive configured".
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


def _load_directive_payload(path: Path) -> dict:
    """Load + validate the directive file at ``path``.

    Raises ``DirectiveLoadFailure`` for every distinct failure
    mode so the supervisor can log the precise reason. The
    failure modes are:

    - ``"unreadable"`` — ``OSError`` reading the file.
    - ``"invalid_json"`` — content is not valid JSON.
    - ``"non_dict_payload"`` — JSON top-level is not a dict.
    - ``"missing_field:<name>"`` — required field absent.
    - ``"wrong_schema_version"`` — schema_version mismatch.
    - ``"digest_mismatch"`` — recomputed digest != stored _sha256.
    """
    try:
        text = path.read_text()
    except OSError as e:
        raise DirectiveLoadFailure(f"unreadable: {e!r}", path) from e
    try:
        directive = json.loads(text)
    except json.JSONDecodeError as e:
        raise DirectiveLoadFailure(f"invalid_json: {e!r}", path) from e
    if not isinstance(directive, dict):
        raise DirectiveLoadFailure(
            f"non_dict_payload: top-level is {type(directive).__name__}, not dict",
            path,
        )
    required = (
        "schema_version", "directive_id", "round_index",
        "head_sha", "repo", "pr_number", "summary",
        "findings",
    )
    for field_name in required:
        if field_name not in directive:
            raise DirectiveLoadFailure(
                f"missing_field:{field_name}", path,
            )
    if directive["schema_version"] != _RELAY_SCHEMA_VERSION:
        raise DirectiveLoadFailure(
            f"wrong_schema_version: {directive['schema_version']!r} is not "
            f"{_RELAY_SCHEMA_VERSION!r}",
            path,
        )
    # Apply the relay's escalation guards. A directive whose
    # findings list contains P0_ESCALATE or whose body
    # strings contain a destructive keyword MUST NOT drive
    # the supervisor. The relay's build_directive refuses
    # these at write time; the bridge refuses them at read
    # time so a hand-edited directive cannot bypass the
    # human-only authority via the directive feed.
    findings = directive.get("findings")
    if not isinstance(findings, list):
        raise DirectiveLoadFailure(
            f"findings_field_invalid: type={type(findings).__name__}",
            path,
        )
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            raise DirectiveLoadFailure(
                f"findings_entry_invalid: index={index} "
                f"type={type(finding).__name__}",
                path,
            )
        severity = str(finding.get("severity") or "")
        if severity == "P0_ESCALATE":
            raise DirectiveLoadFailure(
                f"p0_escalation_in_directive: title={finding.get('title', '')[:80]!r}",
                path,
            )
        body = str(finding.get("body") or "").lower()
        for kw in _ESCALATION_KEYWORDS:
            if kw in body:
                raise DirectiveLoadFailure(
                    f"escalation_keyword_in_directive: keyword={kw!r} finding={finding.get('finding_id', '')[:80]!r}",
                    path,
                )
    # Verify the directive's stored _sha256 against its payload.
    # The canonical digest is the SHA-256 of the canonical
    # serialization of the directive fields, excluding the
    # persisted metadata ``_sha256`` itself.
    stored_sha = directive.get("_sha256")
    if not stored_sha or not isinstance(stored_sha, str):
        raise DirectiveLoadFailure(
            "missing_or_invalid_digest: directive body has no _sha256",
            path,
        )
    canonical_fields = {
        k: v for k, v in directive.items() if k != "_sha256"
    }
    canonical = json.dumps(
        canonical_fields, sort_keys=True, separators=(",", ":"),
    )
    recomputed = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if stored_sha != recomputed:
        raise DirectiveLoadFailure(
            f"digest_mismatch: stored={stored_sha[:12]}.. "
            f"recomputed={recomputed[:12]}..",
            path,
        )
    # Verify the canonical directive .sha256 sidecar file
    # matches the on-disk content. The sidecar is the
    # publish-side artifact indicator; a directive without a
    # verifiable sidecar is not yet durable and cannot
    # launch a worker. The artifact writer stores the sidecar
    # at ``<directive>.sha256`` (sibling file, not suffix
    # replacement).
    sidecar_path = Path(str(path) + ".sha256")
    expected_sidecar = stored_sha + "\n"
    try:
        actual_sidecar = sidecar_path.read_text()
    except OSError as exc:
        raise DirectiveLoadFailure(
            f"sidecar_unreadable: {exc!r}",
            path,
        )
    if actual_sidecar != expected_sidecar:
        raise DirectiveLoadFailure(
            f"sidecar_mismatch: expected={expected_sidecar[:64]!r} "
            f"actual={actual_sidecar[:64]!r}",
            path,
        )
    return directive


def resolve_directive(
    *,
    expected_head: Optional[str] = None,
    directive_path: Optional[str] = None,
) -> Optional[ResolvedDirective]:
    """Resolve the relay-authored directive, if present and valid.

    Returns ``None`` when no directive is configured. Raises
    ``DirectiveLoadFailure`` when a directive is configured but
    invalid so the supervisor can log the precise failure mode
    and fall back to the operator-supplied resume prompt.

    When ``expected_head`` is supplied, directives whose
    ``head_sha`` does not match are rejected with reason
    ``head_mismatch`` (invariant I-08: review evidence is bound
    to the exact current head).
    """
    path = _resolve_directive_path(directive_path)
    if path is None:
        return None
    directive = _load_directive_payload(path)
    if expected_head is not None:
        if directive["head_sha"] != expected_head:
            raise DirectiveLoadFailure(
                f"head_mismatch: directive head {directive['head_sha']!r} "
                f"!= expected {expected_head!r}",
                path,
            )
    prompt = render_directive_prompt(directive)
    stored_sha = directive["_sha256"]
    return ResolvedDirective(
        path=path, prompt=prompt, directive_sha256=stored_sha,
    )


def resolve_worker_prompt(
    *,
    expected_head: Optional[str] = None,
    directive_path: Optional[str] = None,
) -> Optional[str]:
    """Return the relay's worker prompt when a directive is on disk.

    Returns ``None`` when no directive is configured. Does NOT
    raise on directive load failures — callers that need the
    failure mode should use ``resolve_directive`` instead.

    The supervisor's main loop logs the consultation site so
    the absence is observable.
    """
    try:
        resolved = resolve_directive(
            expected_head=expected_head, directive_path=directive_path,
        )
    except DirectiveLoadFailure:
        return None
    if resolved is None:
        return None
    return resolved.prompt


__all__ = [
    "DirectiveLoadFailure",
    "ResolvedDirective",
    "resolve_directive",
    "resolve_worker_prompt",
]
