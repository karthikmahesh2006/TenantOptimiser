"""Unit test enforcing that documentation and config.py agree on all single-source constants."""

import re
from pathlib import Path

from alloc_platform.allocator.adaptive import AdaptiveParams
from alloc_platform.config import config


def test_adaptive_params_defaults_match_config():
    """Verify that AdaptiveParams pulls defaults strictly from config.py."""
    p = AdaptiveParams()
    assert p.memory_safety_mb == config.memory_safety_mb == 64.0
    assert p.throttle_threshold == config.throttle_threshold == 0.05
    assert p.max_shrink_per_epoch == config.max_shrink_per_epoch == 0.20
    assert p.deadband == config.deadband == 0.05
    assert p.min_dwell_s == config.min_dwell_s == 3.0
    assert p.epoch_s == config.epoch_interval_s == 1.0
    assert p.headroom_h == config.headroom_h == 0.20
    assert p.ewma_alpha == config.ewma_alpha == 0.3
    assert p.lambda_churn == config.lambda_churn == 0.10


def test_pool_budget_is_at_most_75_percent():
    """Verify that default pool budget is capped at 75% of host capacity."""
    assert config.default_host_cpu_ratio <= 0.75
    assert config.default_host_mem_ratio <= 0.75
    assert config.default_pool_cpu == round(config.host_vcpus * config.default_host_cpu_ratio, 2)
    assert config.default_pool_mem_mb == round(config.host_mem_mb * config.default_host_mem_ratio, 1)


def test_docs_and_config_agreement():
    """Verify that docs/METHODOLOGY.md matches config.py constants exactly."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    methodology_path = repo_root / "docs" / "METHODOLOGY.md"
    assert methodology_path.exists(), f"METHODOLOGY.md not found at {methodology_path}"

    text = methodology_path.read_text(encoding="utf-8")

    # 1. Memory safety margin: 64 MB
    assert "64" in text and "MB" in text
    assert f"config.memory_safety_mb = {config.memory_safety_mb}" in text

    # 2. Throttle threshold: 5% or 0.05
    assert f"config.throttle_threshold = {config.throttle_threshold}" in text

    # 3. Contraction cap: 20% / 0.20
    assert f"config.max_shrink_per_epoch = {config.max_shrink_per_epoch}" in text
    assert "20%" in text or "20\\%" in text
    # Ensure obsolete 30% contraction cap is NOT present
    assert "30%" not in text and "30\\%" not in text
    assert "30% shrink" not in text
    assert "30% contraction" not in text

    # 4. Deadband: 5% / 0.05
    assert f"config.deadband = {config.deadband}" in text

    # 5. Min dwell: 3 s / 3.0
    assert f"config.min_dwell_s = {config.min_dwell_s}" in text

    # 6. Epoch interval: 1.0 s
    assert f"config.epoch_interval_s = {config.epoch_interval_s}" in text

    # 7. Pool budget: 75%
    assert f"config.default_host_cpu_ratio = {config.default_host_cpu_ratio}" in text
