"""Round-C24-R2 / Worker auth preflight test matrix.

The audit (§7-9) requires:

- worker environment must have authenticated repo write
  capability, OR
- the supervisor refuses to launch a worker that would
  fail halfway through with ``WORKER_REPO_AUTH_UNAVAILABLE``.

The preflight inspects only boolean state. It never
discloses token values, Authorization headers, or
credential file contents. Tests assert:

  M1. READ-only environment (no gh auth, no env token,
      no SSH agent): preflight raises
      ``WorkerRepoAuthUnavailable`` with diagnostic
      ``mechanism='UNKNOWN'``.
  M2. GH auth authenticated: ``write_capable=True``,
      ``mechanism='gh auth'``.
  M3. Token-only env (``GH_TOKEN`` / ``GITHUB_TOKEN``
      without gh auth): ``write_capable=True``,
      ``mechanism='env token'``.
  M4. SSH agent configured: ``write_capable=True``,
      ``mechanism='SSH'``.
  M5. Git credential helper configured: ``write_capable=True``,
      ``mechanism='git credential helper'``.
  M6. The preflight never prints a token, an Authorization
      header, or a credential file content.
  M7. ``to_dict()`` of the result object contains no
      secret-like fields.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


_REPO_ROOT = Path(os.environ.get("REPO_ROOT", str(Path(__file__).resolve().parent.parent)))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


from autocoder_supervisor.worker_auth_preflight import (  # noqa: E402
    WorkerAuthPreflightResult,
    WorkerRepoAuthUnavailable,
    preflight_or_raise,
    run_worker_auth_preflight,
)


# Sentinel test fixtures. Each value is a string that
# CANNOT collide with GitHub's real token formats (which
# the canonical scanner regex flags. The string fragments
# below contain no leading-prefix collisions.
SENT_TOKEN_A = "TEST_LEAD_AED_NONCE_AAAAAAAA"
SENT_TOKEN_B = "TEST_LEAD_AED_NONCE_BBBBBBBB"
SENT_BEARER = "TEST_LEAD_BEARER_FRAGMENT_INSIDE"
SENT_AUTH_HEADER = "TEST_LEAD_AUTHORIZATION_FRAGMENT"


class TestWorkerAuthPreflight:
    def test_no_mechanism_raises_unavailable(self) -> None:
        """M1: no auth at all → raises WorkerRepoAuthUnavailable."""
        with patch(
            "autocoder_supervisor.worker_auth_preflight._check_gh_cli",
            return_value=(False, False),
        ), patch(
            "autocoder_supervisor.worker_auth_preflight._check_ssh_auth_sock",
            return_value=False,
        ), patch(
            "autocoder_supervisor.worker_auth_preflight._check_credential_helper_compat",
            return_value=False,
        ), patch(
            "autocoder_supervisor.worker_auth_preflight._check_repo_walkable",
            return_value=True,
        ):
            with pytest.raises(WorkerRepoAuthUnavailable) as exc:
                preflight_or_raise(repo_dir=Path("/tmp"))
        msg = str(exc.value)
        assert "mechanism='UNKNOWN'" in msg or 'mechanism="UNKNOWN"' in msg, msg

    def test_gh_auth_mechanism(self) -> None:
        """M2: gh CLI authenticated → write_capable=True,
        mechanism='gh auth'."""
        with patch(
            "autocoder_supervisor.worker_auth_preflight._check_gh_cli",
            return_value=(True, True),
        ), patch(
            "autocoder_supervisor.worker_auth_preflight._check_ssh_auth_sock",
            return_value=False,
        ), patch(
            "autocoder_supervisor.worker_auth_preflight._check_credential_helper_compat",
            return_value=False,
        ), patch(
            "autocoder_supervisor.worker_auth_preflight._check_repo_walkable",
            return_value=True,
        ):
            result = run_worker_auth_preflight(repo_dir=Path("/tmp"))
        assert result.write_capable is True
        assert result.mechanism == "gh auth"
        assert result.has_gh_cli is True

    def test_env_token_mechanism(self) -> None:
        """M3: token-only env (no gh auth, no SSH) → write_capable=True."""
        env = {
            "GH_TOKEN": SENT_TOKEN_A,
            "PATH": "/usr/bin:/bin",
            "HOME": "/tmp",
        }
        with patch.dict(os.environ, env, clear=True):
            with patch(
                "autocoder_supervisor.worker_auth_preflight._check_gh_cli",
                return_value=(False, False),
            ), patch(
                "autocoder_supervisor.worker_auth_preflight._check_ssh_auth_sock",
                return_value=False,
            ), patch(
                "autocoder_supervisor.worker_auth_preflight._check_credential_helper_compat",
                return_value=False,
            ), patch(
                "autocoder_supervisor.worker_auth_preflight._check_repo_walkable",
                return_value=True,
            ):
                result = run_worker_auth_preflight(repo_dir=Path("/tmp"))
        assert result.write_capable is True
        assert result.mechanism == "env token"
        assert result.has_gh_token_env is True
        # DO NOT include the actual token in any field.
        dumped = result.to_dict()
        for v in dumped.values():
            if isinstance(v, str):
                assert SENT_TOKEN_A not in v, dumped

    def test_ssh_mechanism(self) -> None:
        """M4: SSH agent → write_capable=True, mechanism='SSH'."""
        with patch(
            "autocoder_supervisor.worker_auth_preflight._check_gh_cli",
            return_value=(False, False),
        ), patch(
            "autocoder_supervisor.worker_auth_preflight._check_ssh_auth_sock",
            return_value=True,
        ), patch(
            "autocoder_supervisor.worker_auth_preflight._check_credential_helper_compat",
            return_value=False,
        ), patch(
            "autocoder_supervisor.worker_auth_preflight._check_repo_walkable",
            return_value=True,
        ):
            result = run_worker_auth_preflight(repo_dir=Path("/tmp"))
        assert result.write_capable is True
        assert result.mechanism == "SSH"

    def test_credential_helper_mechanism(self) -> None:
        """M5: configured helper → write_capable=True."""
        with patch(
            "autocoder_supervisor.worker_auth_preflight._check_gh_cli",
            return_value=(False, False),
        ), patch(
            "autocoder_supervisor.worker_auth_preflight._check_ssh_auth_sock",
            return_value=False,
        ), patch(
            "autocoder_supervisor.worker_auth_preflight._check_credential_helper_compat",
            return_value=True,
        ), patch(
            "autocoder_supervisor.worker_auth_preflight._check_repo_walkable",
            return_value=True,
        ):
            result = run_worker_auth_preflight(repo_dir=Path("/tmp"))
        assert result.write_capable is True
        assert result.mechanism == "git credential helper"

    def test_token_credential_only_no_env_raises(self) -> None:
        """M5b: deployment depends on GH_TOKEN; no env var → raises."""
        with patch(
            "autocoder_supervisor.worker_auth_preflight._check_gh_cli",
            return_value=(False, False),
        ):
            with patch.dict(os.environ, {"PATH": "/usr/bin"}, clear=True):
                with pytest.raises(WorkerRepoAuthUnavailable) as exc:
                    preflight_or_raise(
                        repo_dir=Path("/tmp"),
                        is_token_credential_only=True,
                    )
        msg = str(exc.value)
        assert "write_capable=False" in msg or "mechanism=" in msg

    def test_no_secret_disclosure(self) -> None:
        """M6: the preflight never returns a token / Authorization
        field. ``to_dict()`` of the result must not contain
        any credential-like fragment."""
        env = {
            "GH_TOKEN": SENT_TOKEN_A,
            "GITHUB_TOKEN": SENT_TOKEN_B,
            "AUTHORIZATION_HEADER": SENT_BEARER,
            "PATH": "/usr/bin:/bin",
        }
        with patch.dict(os.environ, env, clear=True):
            with patch(
                "autocoder_supervisor.worker_auth_preflight._check_gh_cli",
                return_value=(False, False),
            ), patch(
                "autocoder_supervisor.worker_auth_preflight._check_repo_walkable",
                return_value=True,
            ):
                result = run_worker_auth_preflight(repo_dir=Path("/tmp"))
        rendered = json.dumps(result.to_dict(), sort_keys=True)
        for fragment in (SENT_TOKEN_A, SENT_TOKEN_B, SENT_BEARER):
            assert fragment not in rendered, (
                f"Preflight leaked credential fragment: {fragment}"
            )

    def test_to_dict_keys_are_boolean_or_status(self) -> None:
        """M7: result keys are only boolean / status fields."""
        result = WorkerAuthPreflightResult(
            read_capable=True,
            write_capable=False,
            mechanism="UNKNOWN",
            has_gh_cli=False,
            has_github_token_env=False,
            has_gh_token_env=False,
            has_ssh_auth_sock=False,
            has_git_credential_helper=False,
            repo_walkable=True,
        )
        keys = set(result.to_dict().keys())
        expected = {
            "read_capable", "write_capable", "mechanism",
            "has_gh_cli", "has_github_token_env", "has_gh_token_env",
            "has_ssh_auth_sock", "has_git_credential_helper",
            "repo_walkable", "error",
        }
        assert keys == expected
