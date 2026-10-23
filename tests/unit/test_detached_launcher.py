import json
import os
import re
import sys
from unittest.mock import MagicMock, patch

repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

import pytest

from alloc_platform.orchestrator.benchmark_matrix import (
    build_plan_tuples,
    compute_watchdog_minutes,
)
from scripts.launch_detached import (
    build_detached_user_data,
    compute_s3_keys,
    verify_iam_put_object_permission,
    verify_security_group_zero_inbound,
)


def test_watchdog_arithmetic_reduced30():
    """Watchdog = runs x (duration + measured_overhead) + 25 min buffer."""
    plan = build_plan_tuples(preset="reduced30")
    assert len(plan) == 30
    duration_s = 120
    overhead_s = 5.0
    buffer_min = 25

    watchdog_min, explanation = compute_watchdog_minutes(
        runs=len(plan), duration_s=duration_s, overhead_s=overhead_s, buffer_min=buffer_min
    )
    # 30 * (120 + 5) = 3750s = 62.5 min. 62.5 + 25 = 87.5 -> ceil = 88 min
    assert watchdog_min == 88
    assert "88 minutes" in explanation
    assert "30 runs x (120s duration + 5.0s overhead)" in explanation


def test_watchdog_arithmetic_smoke3():
    """Watchdog for 3-run smoke test."""
    plan = build_plan_tuples(preset="smoke3")
    assert len(plan) == 3
    watchdog_min, explanation = compute_watchdog_minutes(
        runs=len(plan), duration_s=120, overhead_s=5.0, buffer_min=25
    )
    # 3 * (120 + 5) = 375s = 6.25 min. 6.25 + 25 = 31.25 -> ceil = 32 min
    assert watchdog_min == 32
    assert "32 minutes" in explanation


def test_smoke3_uses_throwaway_seed_9001():
    """Smoke test must use throwaway non-confirmatory seed 9001 outside 42-46."""
    plan = build_plan_tuples(preset="smoke3")
    assert len(plan) == 3
    for pol, sc, seed in plan:
        assert seed == 9001, f"Seed {seed} must be 9001 (throwaway non-confirmatory)"
        assert seed not in [42, 43, 44, 45, 46], "Smoke test must not use confirmatory seeds 42-46"


def test_watchdog_arithmetic_with_custom_overhead():
    """Verify parameterized overhead calculation if overhead is increased after smoke test."""
    plan = build_plan_tuples(preset="reduced30")
    watchdog_min, _ = compute_watchdog_minutes(
        runs=len(plan), duration_s=120, overhead_s=15.0, buffer_min=25
    )
    # 30 * 135 = 4050s = 67.5 min. 67.5 + 25 = 92.5 -> ceil = 93 min
    assert watchdog_min == 93


def test_user_data_pinned_commit_present():
    """User-data builder must check out the exact pinned commit and verify it."""
    test_commit = "abcdef1234567890deadbeef1234567890abcdef"
    ud = build_detached_user_data(
        watchdog_minutes=88,
        pinned_commit=test_commit,
        bucket="test-bucket",
        region="ap-south-1",
        preset="reduced30",
        s3_prefix="backups/stage6/abcdef123456",
    )
    assert f'git checkout "{test_commit}"' in ud
    assert f'if [ "$CHECKED_OUT_HASH" != "{test_commit}" ]; then' in ud
    assert f'echo "Target Commit: {test_commit}"' in ud


def test_user_data_ensures_git_installed():
    """User-data must verify git presence and install via dnf if absent."""
    ud = build_detached_user_data(
        watchdog_minutes=88,
        pinned_commit="abc1234",
        bucket="test-bucket",
        region="ap-south-1",
        preset="reduced30",
        s3_prefix="backups/stage6/abc123456789",
    )
    assert "command -v git" in ud
    assert "dnf install -y git" in ud


def test_user_data_watchdog_value_matches():
    """User-data must set the watchdog shutdown command matching computed minutes."""
    ud = build_detached_user_data(
        watchdog_minutes=88,
        pinned_commit="abc1234",
        bucket="test-bucket",
        region="ap-south-1",
        preset="reduced30",
        s3_prefix="backups/stage6/abc123456789",
    )
    assert "sudo shutdown -h +88" in ud


def test_user_data_shutdown_trap_on_success_and_failure():
    """User-data must set an EXIT trap that executes shutdown -h now on both success and error."""
    ud = build_detached_user_data(
        watchdog_minutes=88,
        pinned_commit="abc1234",
        bucket="test-bucket",
        region="ap-south-1",
        preset="reduced30",
        s3_prefix="backups/stage6/abc123456789",
    )
    assert "trap cleanup_and_shutdown EXIT" in ud
    assert "cleanup_and_shutdown()" in ud
    assert "sudo shutdown -h now" in ud
    # Ensures export of bundle, DB, done.json, and stage6.log before shutdown
    assert "recorded_fallback_stage6.json" in ud
    assert "alloc_stage6.db" in ud
    assert "done.json" in ud
    assert "stage6.log" in ud


def test_user_data_no_secrets():
    """User-data text must not contain AWS secret keys, access key IDs, or tokens."""
    ud = build_detached_user_data(
        watchdog_minutes=88,
        pinned_commit="abc1234",
        bucket="test-bucket",
        region="ap-south-1",
        preset="reduced30",
        s3_prefix="backups/stage6/abc123456789",
    )
    # Check for AWS Access Key pattern (AKIA followed by 16 alphanumeric characters)
    assert not re.search(r"AKIA[0-9A-Z]{16}", ud)
    # Check for common secret keywords being assigned real secrets
    assert "aws_secret_access_key" not in ud.lower()
    assert "aws_session_token" not in ud.lower()


def test_first_upload_failure_aborts():
    """Verify that benchmark_matrix aborts loudly if the first S3 upload fails."""
    import inspect
    from alloc_platform.orchestrator import benchmark_matrix
    src = inspect.getsource(benchmark_matrix.main)
    assert "if idx == 1:" in src
    assert "First S3 upload failed" in src
    assert "write_and_upload_done_file" in src
    assert "status=\"failed\"" in src
    assert "raise RuntimeError" in src


def test_compute_s3_keys():
    """Verify S3 keys generated for reduced30 and smoke3 under backups/stage6/<commit12> scheme."""
    keys30 = compute_s3_keys("my-bucket", "backups/stage6/4f7d15871beb", "reduced30")
    assert "backups/stage6/4f7d15871beb/progress.json" in keys30
    assert "backups/stage6/4f7d15871beb/done.json" in keys30
    assert "backups/stage6/4f7d15871beb/stage6.log" in keys30
    # 5 base keys + 30 run keys = 35 keys
    assert len(keys30) == 35

    keys3 = compute_s3_keys("my-bucket", "backups/stage6/4f7d15871beb", "smoke3")
    # 5 base keys + 3 run keys = 8 keys
    assert len(keys3) == 8


def test_verify_security_group_zero_inbound_dry_run():
    """Verify zero-inbound dry run returns 'dry-run' string without network call."""
    res = verify_security_group_zero_inbound("sg-test", "ap-south-1", dry_run=True)
    assert res == "dry-run"


def test_verify_iam_put_object_permission_mocked_denial():
    """Preflight check must raise PermissionError if IAM simulation denies s3:PutObject."""
    denied_json = {
        "EvaluationResults": [
            {
                "EvalActionName": "s3:PutObject",
                "EvalResourceName": "arn:aws:s3:::bucket/artifacts/stage6/test.json",
                "EvalDecision": "implicitDeny"
            }
        ]
    }
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout=json.dumps(denied_json), returncode=0)
        with pytest.raises(PermissionError) as exc_info:
            verify_iam_put_object_permission(
                role_arn="arn:aws:iam::123:role/test-role",
                target_s3_arn="arn:aws:s3:::bucket/artifacts/stage6/test.json",
                dry_run=False,
            )
        assert "PREFLIGHT IAM CHECK FAILED" in str(exc_info.value)
        assert "implicitDeny" in str(exc_info.value)


def test_verify_iam_put_object_permission_mocked_allowed():
    """Preflight check returns 'allowed' if IAM simulation allows s3:PutObject."""
    allowed_json = {
        "EvaluationResults": [
            {
                "EvalActionName": "s3:PutObject",
                "EvalResourceName": "arn:aws:s3:::bucket/backups/stage6/test.json",
                "EvalDecision": "allowed"
            }
        ]
    }
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout=json.dumps(allowed_json), returncode=0)
        res = verify_iam_put_object_permission(
            role_arn="arn:aws:iam::123:role/test-role",
            target_s3_arn="arn:aws:s3:::bucket/backups/stage6/test.json",
            dry_run=False,
        )
        assert res == "allowed"
