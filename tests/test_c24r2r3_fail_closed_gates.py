"""Round-C24-R2R3 / Fail-closed reviewer-plan and auth-preflight gates.

Two autonomy-correctness defects remained on head 41d577f after the
second CodeRabbit review:

  DEFECT A — ``ReviewerTriggerPlan.required`` defaulted to ``False``.
      The readiness gate skips entries whose ``required is False``,
      so any future producer constructing a plan without stamping
      ``required=True`` silently downgraded a REQUIRED reviewer to
      an OPTIONAL one (fail-open). The default is now ``True``;
      optional providers must construct with ``required=False``
      explicitly.

  DEFECT B — ``launch_worker`` treated an ImportError from the lazy
      ``worker_auth_preflight`` import as "skip preflight"
      (``preflight_or_raise = None`` then continue). That disabled
      the production auth gate: an unavailable verification module
      became permission to launch without any positive repo-write
      check. The ImportError path now FAILS CLOSED (worker not
      launched, explicit diagnostic); the only bypass remains the
      explicit non-production escape hatch
      ``AED_SKIP_WORKER_AUTH_PREFLIGHT=1``.

Standalone-launch regression matrix (audit §4 A-F) is covered by
TestAuthPreflightLaunchMatrix.
"""
from __future__ import annotations

import dataclasses
import json
import os
import sys
from pathlib import Path

import pytest


_REPO_ROOT = Path(os.environ.get(
    "REPO_ROOT", str(Path(__file__).resolve().parent.parent),
))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


from autocoder_supervisor import reviewer_policy as policy  # noqa: E402


HEAD = "a" * 40


# ---------------------------------------------------------------------------
# DEFECT A — ReviewerTriggerPlan fail-closed default
# ---------------------------------------------------------------------------

class TestReviewerTriggerPlanDefault:
    def test_unstamped_required_defaults_true(self) -> None:
        """A plan constructed WITHOUT ``required=`` degrades to
        REQUIRED (fail closed), never to optional."""
        plan = policy.ReviewerTriggerPlan(
            provider="codex",
            action="REQUEST",
            reason="freshness_stale",
        )
        assert plan.required is True

    def test_asdict_roundtrip_preserves_default(self) -> None:
        """The unstamped default survives the ``dataclasses.asdict``
        conversion the supervisor stamps onto snapshots."""
        d = dataclasses.asdict(policy.ReviewerTriggerPlan(
            provider="codex", action="REQUEST", reason="r",
        ))
        assert d["required"] is True

    def test_explicit_optional_still_possible(self) -> None:
        """Optional providers must opt out EXPLICITLY."""
        plan = policy.ReviewerTriggerPlan(
            provider="sourcery",
            action="NOT_NEEDED",
            reason="optional_provider_stale_acceptable",
            required=False,
        )
        assert plan.required is False


def _clean_snap(head: str = HEAD) -> dict:
    """Minimal snapshot that passes every gate ahead of the
    required-reviewer check inside ``evaluate_readiness``."""
    return {
        "head_sha": head,
        "formal_reviews": [],
        "review_threads": {},
        "provider_surfaces": {},
        "providers": {},
        "issue_comments": [],
        "required_checks": {},
    }


class TestReadinessGateFailClosed:
    @pytest.fixture()
    def sup(self, monkeypatch: pytest.MonkeyPatch):
        import autocoder_supervisor.supervisor as sup
        # Deterministic CI verdict so evaluation reaches the
        # required-reviewer gate.
        monkeypatch.setattr(
            sup, "ci_policy_status",
            lambda snap: sup.CI_POLICY_CHECKS_GREEN,
        )
        monkeypatch.setattr(
            sup, "list_unconsumed_events", lambda: [],
        )
        return sup

    def test_unstamped_non_not_needed_plan_blocks(
        self, sup,
    ) -> None:
        """An entry with NO ``required`` key (the forgotten-stamp
        shape) MUST block readiness under the new default."""
        snap = _clean_snap()
        snap["reviewer_plan"] = {
            "codex": {
                "action": "WAITING_FOR_AUTO",
                "reason": "request_cooldown_active",
                # no ``required`` key at all
            },
        }
        result = sup.evaluate_readiness(snap, HEAD)
        assert result.get("ready") is False
        assert result.get("reason") == "required_reviewer_pending"

    def test_explicit_optional_does_not_block(self, sup) -> None:
        """An entry EXPLICITLY marked ``required=False`` stays
        informational and does not stall qualification."""
        snap = _clean_snap()
        snap["reviewer_plan"] = {
            "sourcery": {
                "action": "BLOCK",
                "reason": "optional_provider_paused_acceptable",
                "required": False,
            },
        }
        result = sup.evaluate_readiness(snap, HEAD)
        assert result.get("ready") is True, result

    def test_gate_helper_matches_contract(self, sup) -> None:
        """Direct pin on ``_evaluate_c23_required_blockers``:
        missing key → blocker; explicit False → skipped;
        explicit True → blocker."""
        snap = {
            "reviewer_plan": {
                "codex": {"action": "REQUEST"},               # unstamped
                "sourcery": {"action": "BLOCK",
                             "required": False},              # optional
                "coderabbit": {"action": "WAITING_FOR_AUTO",
                               "required": True},             # explicit
            },
        }
        blockers = sup._evaluate_c23_required_blockers(snap, HEAD)
        providers = sorted(b["provider"] for b in blockers)
        assert providers == ["coderabbit", "codex"]


class TestProductionPlansKeepStamping:
    """The production planner continues to stamp the phase-resolved
    value explicitly on every branch (tests §2 items 4-6)."""

    @pytest.fixture()
    def policies(self):
        return policy.load_policies_from_providers({
            "codex": {},
            "coderabbit": {},
            "sourcery": {},
        })

    def _paused_snap(self) -> dict:
        return {
            "head_sha": HEAD,
            "providers": {
                "codex": {"paused": True},
                "coderabbit": {"paused": True},
                "sourcery": {"paused": True},
            },
            "formal_reviews": [],
            "provider_surfaces": {},
        }

    def test_codex_plans_required_on_both_phases(
        self, policies,
    ) -> None:
        for phase in (
            policy.PHASE_INITIAL_HEAD, policy.PHASE_REPAIR_HEAD,
        ):
            plans = policy.plan_reviewer_actions(
                head_sha=HEAD,
                snap=self._paused_snap(),
                policies={"codex": policies["codex"]},
                phase=phase,
            )
            assert plans["codex"].action == "BLOCK"
            assert plans["codex"].required is True, (
                f"production Codex plan must stay required "
                f"(phase={phase})"
            )

    def test_coderabbit_initial_required_repair_optional(
        self, policies,
    ) -> None:
        initial = policy.plan_reviewer_actions(
            head_sha=HEAD,
            snap=self._paused_snap(),
            policies={"coderabbit": policies["coderabbit"]},
            phase=policy.PHASE_INITIAL_HEAD,
        )
        assert initial["coderabbit"].required is True

        repair = policy.plan_reviewer_actions(
            head_sha=HEAD,
            snap=self._paused_snap(),
            policies={"coderabbit": policies["coderabbit"]},
            phase=policy.PHASE_REPAIR_HEAD,
        )
        assert repair["coderabbit"].action == "NOT_NEEDED"
        assert repair["coderabbit"].required is False, (
            "production CodeRabbit repair-head plan must remain "
            "explicitly optional"
        )

    def test_sourcery_remains_explicitly_optional(
        self, policies,
    ) -> None:
        for phase in (
            policy.PHASE_INITIAL_HEAD, policy.PHASE_REPAIR_HEAD,
        ):
            plans = policy.plan_reviewer_actions(
                head_sha=HEAD,
                snap=self._paused_snap(),
                policies={"sourcery": policies["sourcery"]},
                phase=phase,
            )
            assert plans["sourcery"].action == "NOT_NEEDED"
            assert plans["sourcery"].required is False


# ---------------------------------------------------------------------------
# DEFECT B — auth-preflight ImportError fails closed
# ---------------------------------------------------------------------------

class _BlockPreflightImport:
    """A meta-path finder that makes
    ``autocoder_supervisor.worker_auth_preflight`` unimportable —
    the true-to-production 'module genuinely unavailable' condition
    of standalone/synthetic-package supervisor launches."""

    _TARGET = "autocoder_supervisor.worker_auth_preflight"

    def find_spec(self, fullname, path=None, target=None):
        if fullname == self._TARGET:
            raise ModuleNotFoundError(
                f"No module named {fullname!r} (blocked for test)",
                name=fullname,
            )
        return None


def _block_fresh_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove any cached module entry AND install the blocking
    finder, so the next import genuinely fails (a cached
    ``sys.modules`` entry would short-circuit meta_path)."""
    monkeypatch.delitem(
        sys.modules,
        _BlockPreflightImport._TARGET,
        raising=False,
    )
    sys.meta_path.insert(0, _BlockPreflightImport())


def _worker_launches(launched: list) -> list:
    """Discriminate real worker launches from launch_worker's own
    internal subprocess probes (round-40 session resolution also
    goes through Popen). The durable worker dispatch always carries
    ``--attempt-id``."""
    return [
        c for c in launched
        if isinstance(c, (list, tuple))
        and any(str(a).startswith("--attempt-id") for a in c)
    ]


class TestAuthPreflightLaunchMatrix:
    """Audit §4 A-F: prove Popen cannot run when worker auth cannot
    be positively verified, and that the documented bypass is the
    ONLY way around the gate."""

    @pytest.fixture()
    def env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        import subprocess as _sp

        import autocoder_supervisor.supervisor as sup

        # Real throwaway checkout that SATISFIES the round-37
        # identity guard (remote identity matches the configured
        # owner/repo exactly). We deliberately do NOT set
        # ``AED_SKIP_IDENTITY_GUARD`` here: that hatch bypasses the
        # whole combined gate including the auth preflight, which
        # would make tests A-C meaningless.
        repo_dir = tmp_path / "checkout"
        repo_dir.mkdir()

        def _git(*args) -> None:
            _sp.run(
                ["git", "-C", str(repo_dir), *args],
                check=True, capture_output=True,
            )

        _git("init", "-q")
        _git("config", "user.email", "fixture@example.invalid")
        _git("config", "user.name", "r3-fixture")
        (repo_dir / "README.md").write_text("r3 fixture\n")
        _git("add", "-A")
        _git("commit", "-qm", "init")
        _git(
            "remote", "add", "origin",
            "https://github.com/owner/repo.git",
        )
        monkeypatch.setattr(sup, "REPO_DIR", str(repo_dir))

        # The auth-preflight escape hatch stays UNSET so the gate
        # under test actually executes on every path below.
        monkeypatch.delenv(
            "AED_SKIP_WORKER_AUTH_PREFLIGHT", raising=False,
        )
        monkeypatch.setattr(
            sup, "WORKER_COMMAND_TEMPLATE",
            ["echo", "{prompt}", "{session_id}"],
        )
        monkeypatch.setattr(sup, "SESSION_ID", "r3-session")
        monkeypatch.setattr(sup, "INSTANCE_ID", "r3-instance")
        monkeypatch.setattr(sup, "AUTHORITATIVE_HEAD", HEAD)
        monkeypatch.setattr(sup, "PR_NUMBER", 4)
        monkeypatch.setattr(sup, "REPO_OWNER", "owner")
        monkeypatch.setattr(sup, "REPO_NAME", "repo")
        monkeypatch.setenv("AED_EVIDENCE_ROOT", str(tmp_path / "evidence"))
        (tmp_path / "evidence").mkdir(parents=True, exist_ok=True)
        # Log capture + home isolation for ``sup.log``.
        log_path = tmp_path / "supervisor.log"
        monkeypatch.setattr(sup, "LOG_PATH", log_path)
        monkeypatch.setattr(sup, "SUPERVISOR_HOME", tmp_path)

        launched: list = []

        class FakePopen:
            """Records every Popen construction. Must remain
            subprocess.run-compatible (communicate/returncode/
            context-manager) because ``subprocess.run`` resolves
            ``Popen`` through the same module globals we patch here;
            the git probes of the identity guard are served with
            plausible output so the guard passes for the RIGHT
            reason and the auth gate under test is what decides."""

            def __init__(self, cmd, *args, **kwargs):
                launched.append(list(cmd))
                self.pid = 99999
                self.returncode = 0
                self.args = list(cmd)
                self._cmd = list(cmd)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def communicate(self, input=None, timeout=None, *a, **k):
                # The guard's probes run with text=True; return str
                # so downstream json.dumps in ``log`` works.
                argv = [str(x) for x in self._cmd]
                if "get-url" in argv:
                    return "https://github.com/owner/repo.git\n", ""
                if "abbrev-ref" in argv:
                    return "feat/test\n", ""
                return "", ""

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                return self.returncode

            def kill(self):
                return None

        def _popen_factory(cmd, *args, **kwargs):
            return FakePopen(cmd, *args, **kwargs)

        monkeypatch.setattr(
            sup.subprocess, "Popen", _popen_factory,
        )
        monkeypatch.setattr(sup, "write_lease", lambda lease: None)
        monkeypatch.setattr(sup, "write_cooldown", lambda: None)
        monkeypatch.setattr(
            sup, "start_time_evidence", lambda pid: {"pid": pid},
        )
        # Hermeticity: the strict dirty-tree guard is orthogonal
        # to this suite and would trip on a mid-development tree.
        monkeypatch.setattr(
            sup, "_check_clean_production_checkout",
            lambda path: (True, [], ""),
        )
        return sup, launched, log_path

    def _read_log(self, log_path: Path) -> str:
        if not log_path.exists():
            return ""
        return log_path.read_text()

    # --- A: import succeeds -> normal preflight runs -------------

    def test_a_import_success_runs_preflight(self, env,
                                             monkeypatch) -> None:
        sup, launched, log_path = env
        import autocoder_supervisor.worker_auth_preflight as wap
        calls: list = []
        monkeypatch.setattr(
            wap, "preflight_or_raise",
            lambda **kwargs: calls.append(kwargs) or True,
        )
        lease = sup.launch_worker(
            {"current_head": HEAD}, {"snapshot": {}},
        )
        assert lease is not None
        assert len(_worker_launches(launched)) == 1, (
            "worker must launch on success path"
        )
        assert calls, "preflight must have been invoked"

    # --- B: ImportError -> NOT launched, fail-closed diagnostic --

    def test_b_import_error_fails_closed_no_popen(self, env,
                                                  monkeypatch) -> None:
        sup, launched, log_path = env
        _block_fresh_import(monkeypatch)
        try:
            lease = sup.launch_worker(
                {"current_head": HEAD}, {"snapshot": {}},
            )
        finally:
            sys.meta_path.pop(0)
        assert lease is None, "unavailable preflight must refuse launch"
        assert _worker_launches(launched) == [], (
            "Popen MUST NOT run when the auth gate cannot verify"
        )
        log_text = self._read_log(log_path)
        assert "WORKER_REPO_AUTH_PREFLIGHT_UNAVAILABLE" in log_text, (
            "explicit fail-closed diagnostic required"
        )
        assert "skipping preflight" not in log_text

    # --- C: WorkerRepoAuthUnavailable -> NOT launched ------------

    def test_c_preflight_refusal_launches_nothing(self, env,
                                                  monkeypatch) -> None:
        sup, launched, log_path = env
        import autocoder_supervisor.worker_auth_preflight as wap

        def refuse(**kwargs):
            raise wap.WorkerRepoAuthUnavailable(
                "no positive repo-write evidence (forced)"
            )

        monkeypatch.setattr(wap, "preflight_or_raise", refuse)
        lease = sup.launch_worker(
            {"current_head": HEAD}, {"snapshot": {}},
        )
        assert lease is None
        assert _worker_launches(launched) == [], (
            "refused auth must never reach Popen"
        )
        assert "WORKER_REPO_AUTH_UNAVAILABLE" in self._read_log(log_path)

    # --- D: explicit escape hatch remains the only bypass --------

    def test_d_documented_bypass_still_works(self, env,
                                             monkeypatch) -> None:
        sup, launched, log_path = env
        monkeypatch.setenv("AED_SKIP_WORKER_AUTH_PREFLIGHT", "1")
        # Even with the module UNIMPORTABLE, the explicit hatch
        # bypasses the gate (unchanged documented behaviour).
        _block_fresh_import(monkeypatch)
        try:
            lease = sup.launch_worker(
                {"current_head": HEAD}, {"snapshot": {}},
            )
        finally:
            sys.meta_path.pop(0)
        assert lease is not None
        assert len(_worker_launches(launched)) == 1
        log_text = self._read_log(log_path)
        assert "WORKER_REPO_AUTH_PREFLIGHT_UNAVAILABLE" not in log_text

    # --- E: package-mode import path remains functional ----------

    def test_e_package_mode_import_functional(self) -> None:
        import autocoder_supervisor.worker_auth_preflight as wap
        assert hasattr(wap, "preflight_or_raise")
        assert hasattr(wap, "WorkerRepoAuthUnavailable")

    # --- F: standalone mode cannot convert ImportError->NameError -

    def test_f_no_nameerror_conversion_in_standalone_mode(
        self, env, monkeypatch,
    ) -> None:
        sup, launched, log_path = env
        _block_fresh_import(monkeypatch)
        try:
            # Pre-R3 code raised NameError here (unbound except
            # name). The repaired code must RETURN cleanly instead.
            lease = sup.launch_worker(
                {"current_head": HEAD}, {"snapshot": {}},
            )
        finally:
            sys.meta_path.pop(0)
        assert lease is None
        assert _worker_launches(launched) == []
        log_text = self._read_log(log_path)
        assert "NameError" not in log_text
        assert "WORKER_REPO_AUTH_PREFLIGHT_UNAVAILABLE" in log_text
