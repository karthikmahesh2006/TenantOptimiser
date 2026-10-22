import os
import re
import sys

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
    )
    assert f'git checkout "{test_commit}"' in ud
    assert f'if [ "$CHECKED_OUT_HASH" != "{test_commit}" ]; then' in ud
    assert f'echo "Target Commit: {test_commit}"' in ud


def test_user_data_watchdog_value_matches():
    """User-data must set the watchdog shutdown command matching computed minutes."""
    ud = build_detached_user_data(
        watchdog_minutes=88,
        pinned_commit="abc1234",
        bucket="test-bucket",
        region="ap-south-1",
        preset="reduced30",
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
    """Verify S3 keys generated for reduced30 and smoke3."""
    keys30 = compute_s3_keys("my-bucket", "artifacts/stage6", "reduced30")
    assert "artifacts/stage6/progress.json" in keys30
    assert "artifacts/stage6/done.json" in keys30
    assert "artifacts/stage6/stage6.log" in keys30
    # 5 base keys + 30 run keys = 35 keys
    assert len(keys30) == 35

    keys3 = compute_s3_keys("my-bucket", "artifacts/stage6", "smoke3")
    # 5 base keys + 3 run keys = 8 keys
    assert len(keys3) == 8


def test_verify_security_group_zero_inbound_dry_run():
    """Verify zero-inbound dry run returns 'dry-run' string without network call."""
    res = verify_security_group_zero_inbound("sg-test", "ap-south-1", dry_run=True)
    assert res == "dry-run"
