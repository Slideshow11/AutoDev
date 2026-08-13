"""Round-54/C22 Defect B: provider-state classification.

The supervisor must classify the latest bot-comment body into exactly one of:

  REVIEW_COMPLETE
  REVIEW_IN_PROGRESS
  AUTO_PAUSED_ACTIVE_DEVELOPMENT
  QUOTA_PAUSED
  UNKNOWN

The observed CodeRabbit state on PR #5 is "Reviews paused" — the branch has
been under active development. The classification must:

  - Recognize "Reviews paused" / "under active development" as
    AUTO_PAUSED_ACTIVE_DEVELOPMENT, NOT as walkthrough/complete.
  - Not collapse AUTO_PAUSED into QUOTA_PAUSED.
  - Recognize the actual CodeRabbit "Review rate limited" body as
    QUOTA_PAUSED (the round-2026-08-08 case).
  - Recognize "Review finished" as REVIEW_COMPLETE.
  - Recognize "Review in progress" as REVIEW_IN_PROGRESS.
  - Recognize an empty body as UNKNOWN.
  - Maintain strict precedence: in-progress > auto-paused > quota
    > complete > unknown.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


# Each test references the real CodeRabbit comment-body shape observed
# on PR #5 on 2026-08-13 (just before the pre-canary repair).

REVIEW_BODY_PAUSED = """<!-- This is an auto-generated reply by CodeRabbit -->
<!-- CodeRabbit review command invocation: cf7c7c7c-1234-5678-9abc-def012345678 -->
`@Slideshow11`: I will review pull request `#5` at head `187a97df0b77`.

## Reviews paused

It looks like this branch is under active development - CodeRabbit is pausing reviews until the branch settles.
"""

REVIEW_BODY_RATE_LIMITED = """<!-- This is an auto-generated reply by CodeRabbit -->
<!-- CodeRabbit review command invocation: cf7c7c7c-1234-5678-9abc-def012345678 -->
<details>
<summary>⚠️ Action not completed</summary>

Review rate limited.

> Note: CodeRabbit is an incremental review system and does not re-review already reviewed commits. This command is applicable only when automatic reviews are paused.

</details>
"""

REVIEW_BODY_FINISHED = """<!-- This is an auto-generated reply by CodeRabbit -->
<!-- CodeRabbit review command invocation: cf7c7c7c-1234-5678-9abc-def012345678 -->
<details>
<summary>✅ Action performed</summary>

Review finished.

> Note: CodeRabbit is an incremental review system and does not re-review already reviewed commits. This command is applicable only when automatic reviews are paused.

</details>
"""

REVIEW_BODY_IN_PROGRESS = """<!-- This is an auto-generated comment by CodeRabbit -->
Review in progress
"""

REVIEW_BODY_RESUMED = """<!-- This is an auto-generated reply by CodeRabbit -->
`@Slideshow11`: Resuming the CodeRabbit review for the round-32 P0 liveness fix at `b4d78c2`.

<details>
<summary>✅ Action performed</summary>

Review finished.
</details>
"""

REVIEW_BODY_BARE_COMMENT_TEMPLATE = """<!-- this is an auto-generated comment: review by CodeRabbit -->
"""

REVIEW_BODY_ARBITRARY_NOISE = """CodeRabbit left a single per-file comment.
This is a granular review of one specific change.
"""


# ---------------------------------------------------------------------------
# 1. The "Reviews paused" body MUST classify as AUTO_PAUSED_ACTIVE_DEVELOPMENT
# ---------------------------------------------------------------------------
def test_reviews_paused_is_auto_paused_active_development():
    from autocoder_supervisor.supervisor import (
        classify_provider_state,
        PROVIDER_STATE_AUTO_PAUSED_ACTIVE_DEVELOPMENT,
        PROVIDERS,
        _apply_config,
        default_config_from_env,
    )
    _apply_config(default_config_from_env())
    PROVIDERS["coderabbit"] = {
        "bot_logins": ["coderabbitai[bot]"],
        "trigger_handle": "@coderabbitai review",
        "quota_patterns": [],
        "use_reviews_api": False,
        "required_for_current_repair_round": True,
        "required_for_final_merge": True,
        "required_for_pr_416": True,
        "quota_reset_at": None,
    }
    state, _ = classify_provider_state("coderabbit", REVIEW_BODY_PAUSED)
    assert state == PROVIDER_STATE_AUTO_PAUSED_ACTIVE_DEVELOPMENT, (
        f'expected AUTO_PAUSED_ACTIVE_DEVELOPMENT, got {state!r}'
    )


# ---------------------------------------------------------------------------
# 2. The "Review rate limited" body MUST classify as QUOTA_PAUSED
# ---------------------------------------------------------------------------
def test_rate_limited_is_quota_paused():
    from autocoder_supervisor.supervisor import (
        classify_provider_state,
        PROVIDER_STATE_QUOTA_PAUSED,
        PROVIDERS,
        _apply_config,
        default_config_from_env,
    )
    _apply_config(default_config_from_env())
    PROVIDERS["coderabbit"] = {
        "bot_logins": ["coderabbitai[bot]"],
        "trigger_handle": "@coderabbitai review",
        "quota_patterns": [],
        "use_reviews_api": False,
        "required_for_current_repair_round": True,
        "required_for_final_merge": True,
        "required_for_pr_416": True,
        "quota_reset_at": None,
    }
    state, _ = classify_provider_state("coderabbit", REVIEW_BODY_RATE_LIMITED)
    assert state == PROVIDER_STATE_QUOTA_PAUSED


# ---------------------------------------------------------------------------
# 3. The "Review finished" body MUST classify as REVIEW_COMPLETE
# ---------------------------------------------------------------------------
def test_review_finished_is_review_complete():
    from autocoder_supervisor.supervisor import (
        classify_provider_state,
        PROVIDER_STATE_REVIEW_COMPLETE,
        PROVIDERS,
        _apply_config,
        default_config_from_env,
    )
    _apply_config(default_config_from_env())
    PROVIDERS["coderabbit"] = {
        "bot_logins": ["coderabbitai[bot]"],
        "trigger_handle": "@coderabbitai review",
        "quota_patterns": [],
        "use_reviews_api": False,
        "required_for_current_repair_round": True,
        "required_for_final_merge": True,
        "required_for_pr_416": True,
        "quota_reset_at": None,
    }
    state, _ = classify_provider_state("coderabbit", REVIEW_BODY_FINISHED)
    assert state == PROVIDER_STATE_REVIEW_COMPLETE


# ---------------------------------------------------------------------------
# 4. "Review in progress" MUST classify as REVIEW_IN_PROGRESS
# ---------------------------------------------------------------------------
def test_review_in_progress_is_in_progress():
    from autocoder_supervisor.supervisor import (
        classify_provider_state,
        PROVIDER_STATE_REVIEW_IN_PROGRESS,
        PROVIDERS,
        _apply_config,
        default_config_from_env,
    )
    _apply_config(default_config_from_env())
    PROVIDERS["coderabbit"] = {
        "bot_logins": ["coderabbitai[bot]"],
        "trigger_handle": "@coderabbitai review",
        "quota_patterns": [],
        "use_reviews_api": False,
        "required_for_current_repair_round": True,
        "required_for_final_merge": True,
        "required_for_pr_416": True,
        "quota_reset_at": None,
    }
    state, _ = classify_provider_state(
        "coderabbit", REVIEW_BODY_IN_PROGRESS
    )
    assert state == PROVIDER_STATE_REVIEW_IN_PROGRESS


# ---------------------------------------------------------------------------
# 5. AUTO_PAUSED must NOT be collapsed into QUOTA_PAUSED
# ---------------------------------------------------------------------------
def test_auto_paused_not_collapsed_with_quota_paused():
    from autocoder_supervisor.supervisor import (
        classify_provider_state,
        PROVIDER_STATE_AUTO_PAUSED_ACTIVE_DEVELOPMENT,
        PROVIDER_STATE_QUOTA_PAUSED,
        PROVIDERS,
        _apply_config,
        default_config_from_env,
    )
    _apply_config(default_config_from_env())
    PROVIDERS["coderabbit"] = {
        "bot_logins": ["coderabbitai[bot]"],
        "trigger_handle": "@coderabbitai review",
        "quota_patterns": [],
        "use_reviews_api": False,
        "required_for_current_repair_round": True,
        "required_for_final_merge": True,
        "required_for_pr_416": True,
        "quota_reset_at": None,
    }
    state_a, _ = classify_provider_state("coderabbit", REVIEW_BODY_PAUSED)
    state_q, _ = classify_provider_state(
        "coderabbit", REVIEW_BODY_RATE_LIMITED
    )
    assert state_a != state_q, (
        "AUTO_PAUSED and QUOTA_PAUSED must be distinct states"
    )
    assert state_a == PROVIDER_STATE_AUTO_PAUSED_ACTIVE_DEVELOPMENT
    assert state_q == PROVIDER_STATE_QUOTA_PAUSED


# ---------------------------------------------------------------------------
# 6. The "Reviews paused" body MUST NOT classify as REVIEW_COMPLETE
# ---------------------------------------------------------------------------
def test_reviews_paused_is_not_review_complete():
    from autocoder_supervisor.supervisor import (
        classify_provider_state,
        PROVIDER_STATE_REVIEW_COMPLETE,
        PROVIDERS,
        _apply_config,
        default_config_from_env,
    )
    _apply_config(default_config_from_env())
    PROVIDERS["coderabbit"] = {
        "bot_logins": ["coderabbitai[bot]"],
        "trigger_handle": "@coderabbitai review",
        "quota_patterns": [],
        "use_reviews_api": False,
        "required_for_current_repair_round": True,
        "required_for_final_merge": True,
        "required_for_pr_416": True,
        "quota_reset_at": None,
    }
    state, _ = classify_provider_state("coderabbit", REVIEW_BODY_PAUSED)
    assert state != PROVIDER_STATE_REVIEW_COMPLETE, (
        "A paused comment must NOT classify as a finished review"
    )


# ---------------------------------------------------------------------------
# 7. The "Reviews paused" body MUST NOT classify as UNKNOWN
# ---------------------------------------------------------------------------
def test_reviews_paused_is_not_unknown():
    from autocoder_supervisor.supervisor import (
        classify_provider_state,
        PROVIDER_STATE_UNKNOWN,
        PROVIDERS,
        _apply_config,
        default_config_from_env,
    )
    _apply_config(default_config_from_env())
    PROVIDERS["coderabbit"] = {
        "bot_logins": ["coderabbitai[bot]"],
        "trigger_handle": "@coderabbitai review",
        "quota_patterns": [],
        "use_reviews_api": False,
        "required_for_current_repair_round": True,
        "required_for_final_merge": True,
        "required_for_pr_416": True,
        "quota_reset_at": None,
    }
    state, _ = classify_provider_state("coderabbit", REVIEW_BODY_PAUSED)
    assert state != PROVIDER_STATE_UNKNOWN, (
        "The paused body must be classified into a specific state"
    )


# ---------------------------------------------------------------------------
# 8. Empty body MUST classify as UNKNOWN
# ---------------------------------------------------------------------------
def test_empty_body_is_unknown():
    from autocoder_supervisor.supervisor import (
        classify_provider_state,
        PROVIDER_STATE_UNKNOWN,
        PROVIDERS,
        _apply_config,
        default_config_from_env,
    )
    _apply_config(default_config_from_env())
    PROVIDERS["coderabbit"] = {
        "bot_logins": ["coderabbitai[bot]"],
        "trigger_handle": "@coderabbitai review",
        "quota_patterns": [],
        "use_reviews_api": False,
        "required_for_current_repair_round": True,
        "required_for_final_merge": True,
        "required_for_pr_416": True,
        "quota_reset_at": None,
    }
    state, _ = classify_provider_state("coderabbit", None)
    assert state == PROVIDER_STATE_UNKNOWN

    state, _ = classify_provider_state("coderabbit", "")
    assert state == PROVIDER_STATE_UNKNOWN


# ---------------------------------------------------------------------------
# 9. A bare comment-template line (no review result) is REVIEW_COMPLETE
# ---------------------------------------------------------------------------
def test_bare_comment_template_is_review_complete():
    from autocoder_supervisor.supervisor import (
        classify_provider_state,
        PROVIDER_STATE_REVIEW_COMPLETE,
        PROVIDERS,
        _apply_config,
        default_config_from_env,
    )
    _apply_config(default_config_from_env())
    PROVIDERS["coderabbit"] = {
        "bot_logins": ["coderabbitai[bot]"],
        "trigger_handle": "@coderabbitai review",
        "quota_patterns": [],
        "use_reviews_api": False,
        "required_for_current_repair_round": True,
        "required_for_final_merge": True,
        "required_for_pr_416": True,
        "quota_reset_at": None,
    }
    state, _ = classify_provider_state(
        "coderabbit", REVIEW_BODY_BARE_COMMENT_TEMPLATE
    )
    assert state == PROVIDER_STATE_REVIEW_COMPLETE


# ---------------------------------------------------------------------------
# 10. The "Resuming CodeRabbit reviews" body (where the bot says it
# is resuming) is REVIEW_COMPLETE
# ---------------------------------------------------------------------------
def test_resuming_comment_is_review_complete():
    from autocoder_supervisor.supervisor import (
        classify_provider_state,
        PROVIDER_STATE_REVIEW_COMPLETE,
        PROVIDERS,
        _apply_config,
        default_config_from_env,
    )
    _apply_config(default_config_from_env())
    PROVIDERS["coderabbit"] = {
        "bot_logins": ["coderabbitai[bot]"],
        "trigger_handle": "@coderabbitai review",
        "quota_patterns": [],
        "use_reviews_api": False,
        "required_for_current_repair_round": True,
        "required_for_final_merge": True,
        "required_for_pr_416": True,
        "quota_reset_at": None,
    }
    state, _ = classify_provider_state(
        "coderabbit", REVIEW_BODY_RESUMED
    )
    assert state == PROVIDER_STATE_REVIEW_COMPLETE


# ---------------------------------------------------------------------------
# 11. A truly arbitrary comment from the bot (no recognizer matches)
# is UNKNOWN. It must NOT be silently classified as REVIEW_COMPLETE.
# ---------------------------------------------------------------------------
def test_arbitrary_comment_is_unknown():
    from autocoder_supervisor.supervisor import (
        classify_provider_state,
        PROVIDER_STATE_UNKNOWN,
        PROVIDERS,
        _apply_config,
        default_config_from_env,
    )
    _apply_config(default_config_from_env())
    PROVIDERS["coderabbit"] = {
        "bot_logins": ["coderabbitai[bot]"],
        "trigger_handle": "@coderabbitai review",
        "quota_patterns": [],
        "use_reviews_api": False,
        "required_for_current_repair_round": True,
        "required_for_final_merge": True,
        "required_for_pr_416": True,
        "quota_reset_at": None,
    }
    state, _ = classify_provider_state(
        "coderabbit", REVIEW_BODY_ARBITRARY_NOISE
    )
    assert state == PROVIDER_STATE_UNKNOWN, (
        "An arbitrary comment with no recognizer matched must be UNKNOWN. "
        "The legacy walkthrough/complete pattern would have falsely "
        "classified this as complete."
    )


# ---------------------------------------------------------------------------
# 12. The precedence is: in-progress > auto-paused > quota > complete
# ---------------------------------------------------------------------------
def test_precedence_in_progress_beats_paused():
    from autocoder_supervisor.supervisor import (
        classify_provider_state,
        PROVIDER_STATE_REVIEW_IN_PROGRESS,
        PROVIDERS,
        _apply_config,
        default_config_from_env,
    )
    _apply_config(default_config_from_env())
    PROVIDERS["coderabbit"] = {
        "bot_logins": ["coderabbitai[bot]"],
        "trigger_handle": "@coderabbitai review",
        "quota_patterns": [],
        "use_reviews_api": False,
        "required_for_current_repair_round": True,
        "required_for_final_merge": True,
        "required_for_pr_416": True,
        "quota_reset_at": None,
    }
    # Body contains both "Review in progress" and "Reviews paused".
    # In-progress wins.
    body = "Review in progress\n\n## Reviews paused"
    state, _ = classify_provider_state("coderabbit", body)
    assert state == PROVIDER_STATE_REVIEW_IN_PROGRESS
