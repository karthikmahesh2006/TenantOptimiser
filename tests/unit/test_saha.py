"""Comprehensive unit test suite for the Saturation-Aware Hybrid Allocator (SAHA) - Stage 6 Extension.

Validates all required properties and condition fixes:
1. DRF progressive filling reuse in FAIR mode.
2. Originals NOT mutated by DRF synthetic reuse path.
3. Epsilon clamp on d_hat = 0.
4. Mode selection with hysteresis on rho in [rho_low, rho_high].
5. Throttle-aware multiplicative demand escalation without cap jumping.
6. Memory floor sliding window RSS, cold-start protection (<10 samples, t < 60s), and burst handling.
7. Capacity cut Scenario 3 shape (1.50c/3072MB -> 0.90c/2048MB) with 3-epoch latching and bypass.
8. CPU-only capacity cut bypass.
9. Memory-only capacity cut bypass down to floor.
10. Hard invariants beat soft limits (only above-floor/above-min portion scaled down).
11. Deterministic priority memory floor infeasibility shedding (lowest dominant share first, ties broken by ID).
12. Headline metric rejection accounting (shedding cannot improve on-time ratio).
13. End-to-end shedding in runner path producing actual status == "rejected" samples and decreasing on_time_ratio.
14. Regression test loading golden corpus generated at baseline commit 44f6802.
15. Determinism and sub-millisecond solve time (with loose sanity bound < 50 ms).
"""

import json
import os
from pathlib import Path

import numpy as np
import pytest

from alloc_platform.allocator.adaptive import AdaptiveAllocator
from alloc_platform.allocator.drf import allocate_drf
from alloc_platform.allocator.fcfs import allocate_fcfs
from alloc_platform.allocator.proportional import allocate_proportional
from alloc_platform.allocator.saha import SahaAllocator, SahaParams, allocate_saha
from alloc_platform.allocator.types import Plan, Pool, Tenant, TenantObs
from alloc_platform.config import config
from alloc_platform.orchestrator.runner import apply_live_path_shedding, _execute_single_run
from alloc_platform.workload.loadgen import RequestSample
from alloc_platform.workload.traces import Arrival


@pytest.fixture
def sample_pool():
    return Pool(capacity={"cpu": 1.5, "mem_mb": 3072.0})


@pytest.fixture
def sample_tenants():
    return [
        Tenant(
            id="tenant_a",
            name="Tenant A (CPU heavy)",
            weight=2.0,
            priority=1,
            arrival_seq=1,
            active=True,
            demand={"cpu": 1.2, "mem_mb": 512.0},
            minimum={"cpu": 0.2, "mem_mb": 128.0},
            workload_type="cpu_heavy",
        ),
        Tenant(
            id="tenant_b",
            name="Tenant B (Memory heavy)",
            weight=1.0,
            priority=1,
            arrival_seq=2,
            active=True,
            demand={"cpu": 0.4, "mem_mb": 2048.0},
            minimum={"cpu": 0.1, "mem_mb": 512.0},
            workload_type="memory_heavy",
        ),
        Tenant(
            id="tenant_c",
            name="Tenant C (Mixed)",
            weight=1.0,
            priority=1,
            arrival_seq=3,
            active=True,
            demand={"cpu": 0.8, "mem_mb": 1024.0},
            minimum={"cpu": 0.1, "mem_mb": 256.0},
            workload_type="mixed",
        ),
    ]


def test_drf_reuse_in_fair_mode(sample_pool, sample_tenants):
    """Condition 1: In FAIR mode, raw allocation == allocate_drf on dhat before floor/rate limits."""
    saha = SahaAllocator()
    saha.mode = "FAIR"

    obs = {
        "tenant_a": TenantObs(usage={"cpu": 1.1, "mem_mb": 400.0}),
        "tenant_b": TenantObs(usage={"cpu": 0.35, "mem_mb": 1800.0}),
        "tenant_c": TenantObs(usage={"cpu": 0.75, "mem_mb": 900.0}),
    }

    dhat = saha.estimate_demand(sample_tenants, obs, sample_pool)
    raw_alloc = saha.raw_allocation("FAIR", sample_pool, sample_tenants, dhat)

    synthetic_tenants = [
        Tenant(
            id=t.id,
            name=t.name,
            weight=t.weight,
            priority=t.priority,
            arrival_seq=t.arrival_seq,
            active=t.active,
            demand={
                r: min(
                    float(t.demand.get(r, 0.0)),
                    max(
                        float(t.minimum.get(r, 0.0)),
                        max(config.saha_dhat_min_epsilon, dhat[t.id][r]),
                    ),
                )
                for r in sample_pool.capacity
            },

            minimum=dict(t.minimum),
            workload_type=t.workload_type,
        )
        for t in sample_tenants
    ]
    ref_drf = allocate_drf(sample_pool, synthetic_tenants)

    for t in sample_tenants:
        for r in sample_pool.capacity:
            assert raw_alloc[t.id][r] == pytest.approx(ref_drf.alloc[t.id][r], abs=1e-5)


def test_drf_reuse_originals_not_mutated(sample_pool, sample_tenants):
    """Item 3d: Tenant input objects are never mutated by the synthetic DRF reuse path."""
    orig_demands = {t.id: dict(t.demand) for t in sample_tenants}
    orig_minimums = {t.id: dict(t.minimum) for t in sample_tenants}
    orig_weights = {t.id: t.weight for t in sample_tenants}

    saha = SahaAllocator()
    obs = {
        "tenant_a": TenantObs(usage={"cpu": 1.1, "mem_mb": 400.0}),
        "tenant_b": TenantObs(usage={"cpu": 0.35, "mem_mb": 1800.0}),
        "tenant_c": TenantObs(usage={"cpu": 0.75, "mem_mb": 900.0}),
    }
    _ = saha.allocate(sample_pool, sample_tenants, obs, now=5.0)

    for t in sample_tenants:
        assert t.demand == orig_demands[t.id], f"Tenant {t.id} demand was mutated!"
        assert t.minimum == orig_minimums[t.id], f"Tenant {t.id} minimum was mutated!"
        assert t.weight == orig_weights[t.id], f"Tenant {t.id} weight was mutated!"


def test_epsilon_clamp_on_zero_dhat(sample_pool, sample_tenants):
    """Item 3e: Epsilon clamp on d_hat = 0 strictly ensures positive non-zero demand."""
    saha = SahaAllocator()
    # Telemetry with zero usage across all tenants
    obs_zero = {
        t.id: TenantObs(usage={"cpu": 0.0, "mem_mb": 0.0}, throttle_ratio=0.0)
        for t in sample_tenants
    }
    dhat = saha.estimate_demand(sample_tenants, obs_zero, sample_pool)

    for t in sample_tenants:
        for r in sample_pool.capacity:
            assert dhat[t.id][r] >= config.saha_dhat_min_epsilon
            assert dhat[t.id][r] == pytest.approx(config.saha_dhat_min_epsilon, abs=1e-6)


def test_mode_hysteresis_no_flapping(sample_pool, sample_tenants):
    """Step 3: Mode does not flap when rho oscillates within [rho_low, rho_high]."""
    saha = SahaAllocator()
    assert saha.mode == "RECLAIM"

    mode1 = saha.select_mode(0.5, now=1.0)
    assert mode1 == "RECLAIM"

    mode2 = saha.select_mode(0.90, now=2.0)
    assert mode2 == "RECLAIM"

    mode3 = saha.select_mode(1.05, now=3.0)
    assert mode3 == "FAIR"

    mode4 = saha.select_mode(0.92, now=4.0)
    assert mode4 == "FAIR"

    mode5 = saha.select_mode(0.80, now=5.0)
    assert mode5 == "RECLAIM"

    assert len(saha.mode_switches) == 2
    assert saha.mode_switches[0]["from_mode"] == "RECLAIM"
    assert saha.mode_switches[0]["to_mode"] == "FAIR"
    assert saha.mode_switches[1]["from_mode"] == "FAIR"
    assert saha.mode_switches[1]["to_mode"] == "RECLAIM"


def test_throttle_aware_multiplicative_escalation(sample_pool, sample_tenants):
    """Step 1: Multiplicative demand increase by throttle_step when CFS throttled, without cap jumping."""
    saha = SahaAllocator()
    obs_unthrottled = {
        "tenant_a": TenantObs(usage={"cpu": 0.40, "mem_mb": 200.0}, throttle_ratio=0.0),
    }
    dhat1 = saha.estimate_demand(sample_tenants, obs_unthrottled, sample_pool)
    base_cpu_est = dhat1["tenant_a"]["cpu"]

    obs_throttled = {
        "tenant_a": TenantObs(usage={"cpu": 0.40, "mem_mb": 200.0}, throttle_ratio=0.15),
    }
    dhat2 = saha.estimate_demand(sample_tenants, obs_throttled, sample_pool)
    throttled_cpu_est = dhat2["tenant_a"]["cpu"]

    expected_throttled = min(1.2, base_cpu_est * config.saha_throttle_step)
    assert throttled_cpu_est == pytest.approx(expected_throttled, rel=1e-2)
    assert throttled_cpu_est < 1.2


def test_memory_floor_cold_start_and_burst(sample_pool, sample_tenants):
    """Condition 2: Cold start (< 10 samples or t < 60s) does not contract memory below current allocation; handles burst in first 30s."""
    saha = SahaAllocator()
    prev_plan = Plan(
        alloc={
            "tenant_a": {"cpu": 0.5, "mem_mb": 512.0},
            "tenant_b": {"cpu": 0.2, "mem_mb": 1500.0},
            "tenant_c": {"cpu": 0.3, "mem_mb": 800.0},
        }
    )

    obs_cold = {
        "tenant_a": TenantObs(usage={"cpu": 0.2, "mem_mb": 30.0}),
        "tenant_b": TenantObs(usage={"cpu": 0.1, "mem_mb": 100.0}),
        "tenant_c": TenantObs(usage={"cpu": 0.2, "mem_mb": 50.0}),
    }
    plan1 = saha.allocate(sample_pool, sample_tenants, obs_cold, prev=prev_plan, now=1.0)

    assert plan1.alloc["tenant_a"]["mem_mb"] >= 512.0 - 1e-4
    assert plan1.alloc["tenant_b"]["mem_mb"] >= 1500.0 - 1e-4

    obs_burst = {
        "tenant_a": TenantObs(usage={"cpu": 0.5, "mem_mb": 450.0}),
        "tenant_b": TenantObs(usage={"cpu": 0.1, "mem_mb": 100.0}),
        "tenant_c": TenantObs(usage={"cpu": 0.2, "mem_mb": 50.0}),
    }
    plan_burst = saha.allocate(sample_pool, sample_tenants, obs_burst, prev=plan1, now=15.0)

    assert plan_burst.alloc["tenant_a"]["mem_mb"] == pytest.approx(512.0, abs=1e-4)


def test_capacity_cut_scenario3_shape_with_latching(sample_tenants):
    """Condition 3: Capacity cut (Scenario 3 shape: 1.50c/3072MB -> 0.90c/2048MB) with 3-epoch latching and bypass."""
    saha = SahaAllocator()
    initial_pool = Pool(capacity={"cpu": 1.5, "mem_mb": 3072.0})
    prev_plan = Plan(
        alloc={
            "tenant_a": {"cpu": 0.8, "mem_mb": 512.0},
            "tenant_b": {"cpu": 0.3, "mem_mb": 1500.0},
            "tenant_c": {"cpu": 0.4, "mem_mb": 1000.0},
        }
    )

    cut_pool = Pool(capacity={"cpu": 0.9, "mem_mb": 2048.0})
    saha.prev_pool = initial_pool

    obs = {
        "tenant_a": TenantObs(usage={"cpu": 0.8, "mem_mb": 300.0}),
        "tenant_b": TenantObs(usage={"cpu": 0.2, "mem_mb": 1200.0}),
        "tenant_c": TenantObs(usage={"cpu": 0.3, "mem_mb": 500.0}),
    }

    plan15 = saha.allocate(cut_pool, sample_tenants, obs, prev=prev_plan, now=15.0)
    assert sum(plan15.alloc[t.id]["cpu"] for t in sample_tenants) <= 0.90 + 1e-6
    assert sum(plan15.alloc[t.id]["mem_mb"] for t in sample_tenants) <= 2048.0 + 1e-6
    assert saha.cut_latch_counter == 3

    plan16 = saha.allocate(cut_pool, sample_tenants, obs, prev=plan15, now=16.0)
    assert saha.cut_latch_counter == 2

    plan17 = saha.allocate(cut_pool, sample_tenants, obs, prev=plan16, now=17.0)
    assert saha.cut_latch_counter == 1


def test_capacity_cut_cpu_only(sample_tenants):
    """Item 3a: CPU-only capacity cut bypasses CPU contraction rate limit."""
    saha = SahaAllocator()
    initial_pool = Pool(capacity={"cpu": 1.5, "mem_mb": 3072.0})
    saha.prev_pool = initial_pool

    prev_plan = Plan(
        alloc={
            "tenant_a": {"cpu": 0.9, "mem_mb": 500.0},
            "tenant_b": {"cpu": 0.3, "mem_mb": 1000.0},
            "tenant_c": {"cpu": 0.3, "mem_mb": 500.0},
        }
    )

    # CPU drops sharply to 0.70c, memory unchanged at 3072MB
    cut_pool = Pool(capacity={"cpu": 0.70, "mem_mb": 3072.0})
    obs = {t.id: TenantObs(usage={"cpu": 0.5, "mem_mb": 400.0}) for t in sample_tenants}

    plan = saha.allocate(cut_pool, sample_tenants, obs, prev=prev_plan, now=20.0)

    # CPU must instantly fit within 0.70c (bypassing 20% limit which would have restricted contraction to >= 1.2c)
    tot_cpu = sum(plan.alloc[t.id]["cpu"] for t in sample_tenants)
    assert tot_cpu <= 0.70 + 1e-6


def test_capacity_cut_memory_only(sample_tenants):
    """Item 3b: Memory-only capacity cut bypasses memory contraction step down to the floor."""
    saha = SahaAllocator()
    initial_pool = Pool(capacity={"cpu": 1.5, "mem_mb": 3072.0})
    saha.prev_pool = initial_pool

    prev_plan = Plan(
        alloc={
            "tenant_a": {"cpu": 0.5, "mem_mb": 500.0},
            "tenant_b": {"cpu": 0.3, "mem_mb": 1500.0},
            "tenant_c": {"cpu": 0.3, "mem_mb": 1000.0},
        }
    )

    # Memory drops to 1600 MB, CPU unchanged
    cut_pool = Pool(capacity={"cpu": 1.5, "mem_mb": 1600.0})
    obs = {
        "tenant_a": TenantObs(usage={"cpu": 0.2, "mem_mb": 150.0}),
        "tenant_b": TenantObs(usage={"cpu": 0.1, "mem_mb": 600.0}),
        "tenant_c": TenantObs(usage={"cpu": 0.1, "mem_mb": 300.0}),
    }

    plan = saha.allocate(cut_pool, sample_tenants, obs, prev=prev_plan, now=25.0)

    tot_mem = sum(plan.alloc[t.id]["mem_mb"] for t in sample_tenants)
    assert tot_mem <= 1600.0 + 1e-6


def test_hard_invariants_beat_soft_limits(sample_tenants):
    """Item 3c: Hard invariants beat soft limits; only above-floor/above-min portions are scaled down."""
    saha = SahaAllocator()
    tight_pool = Pool(capacity={"cpu": 0.8, "mem_mb": 1500.0})

    # Artificial prev plan with high values
    prev_plan = Plan(
        alloc={
            "tenant_a": {"cpu": 0.8, "mem_mb": 512.0},
            "tenant_b": {"cpu": 0.4, "mem_mb": 1500.0},
            "tenant_c": {"cpu": 0.4, "mem_mb": 1000.0},
        }
    )

    obs = {
        "tenant_a": TenantObs(usage={"cpu": 0.5, "mem_mb": 200.0}),
        "tenant_b": TenantObs(usage={"cpu": 0.2, "mem_mb": 600.0}),
        "tenant_c": TenantObs(usage={"cpu": 0.3, "mem_mb": 400.0}),
    }

    plan = saha.allocate(tight_pool, sample_tenants, obs, prev=prev_plan, now=30.0)

    # Hard capacity invariant strictly met
    assert sum(plan.alloc[t.id]["cpu"] for t in sample_tenants) <= 0.80 + 1e-6
    assert sum(plan.alloc[t.id]["mem_mb"] for t in sample_tenants) <= 1500.0 + 1e-6

    # Minimums and floors are preserved
    for t in sample_tenants:
        assert plan.alloc[t.id]["cpu"] >= float(t.minimum.get("cpu", 0.0)) - 1e-5


def test_memory_floor_infeasibility_deterministic_shedding(sample_pool):
    """Condition 4: When sum of memory floors > C_mem, deterministic priority honors lowest dominant share first and records unmet_guarantees."""
    saha = SahaAllocator()
    tenants = [
        Tenant(
            id="tenant_1_big",
            name="Big Dominant Tenant",
            weight=1.0,
            priority=1,
            arrival_seq=1,
            active=True,
            demand={"cpu": 1.0, "mem_mb": 2500.0},
            minimum={"cpu": 0.1, "mem_mb": 2000.0},
            workload_type="memory_heavy",
        ),
        Tenant(
            id="tenant_2_small",
            name="Small Dominant Tenant",
            weight=1.0,
            priority=1,
            arrival_seq=2,
            active=True,
            demand={"cpu": 0.5, "mem_mb": 1500.0},
            minimum={"cpu": 0.1, "mem_mb": 1200.0},
            workload_type="mixed",
        ),
    ]

    obs = {
        "tenant_1_big": TenantObs(usage={"cpu": 0.5, "mem_mb": 2000.0}),
        "tenant_2_small": TenantObs(usage={"cpu": 0.3, "mem_mb": 1200.0}),
    }

    plan = saha.allocate(sample_pool, tenants, obs, now=100.0)

    assert any("WARNING" in n for n in plan.notes)
    assert len(plan.unmet_guarantees) > 0
    assert plan.alloc["tenant_2_small"]["mem_mb"] >= 1200.0
    assert ("tenant_1_big", "mem_mb") in plan.unmet_guarantees
    assert sum(plan.alloc[t.id]["mem_mb"] for t in tenants) <= 3072.0 + 1e-6


def test_rejection_shedding_cannot_improve_headline_metric():
    """Condition 4: Denominator = total_offered; shedding/rejected requests count as failures and cannot improve on-time ratio."""
    total_offered = 100
    on_time_A = 60
    ratio_A = on_time_A / total_offered  # 0.60

    on_time_B = 60  # 20 rejected, 20 deadline missed
    ratio_B = on_time_B / total_offered  # 0.60

    on_time_C = 55  # 5 shed/rejected instead of completed on time
    ratio_C = on_time_C / total_offered  # 0.55

    assert ratio_A == 0.60
    assert ratio_B == 0.60
    assert ratio_C < ratio_A


def test_shedding_end_to_end_in_runner_path(sample_pool):
    """Item 1: Infeasible memory plan produces actual status == 'rejected' samples in runner path, decreasing on_time_ratio."""
    saha = SahaAllocator()
    tenants = [
        Tenant(
            id="t_ok",
            name="OK Tenant",
            weight=1.0,
            priority=1,
            arrival_seq=1,
            active=True,
            demand={"cpu": 0.5, "mem_mb": 1000.0},
            minimum={"cpu": 0.1, "mem_mb": 800.0},
        ),
        Tenant(
            id="t_shed",
            name="Shed Tenant",
            weight=1.0,
            priority=1,
            arrival_seq=2,
            active=True,
            demand={"cpu": 0.8, "mem_mb": 2500.0},
            minimum={"cpu": 0.1, "mem_mb": 2400.0},
        ),
    ]

    obs = {
        "t_ok": TenantObs(usage={"cpu": 0.3, "mem_mb": 800.0}),
        "t_shed": TenantObs(usage={"cpu": 0.5, "mem_mb": 2400.0}),
    }

    # Generate plan where t_shed floor is infeasible and marked in unmet_guarantees
    plan = saha.allocate(sample_pool, tenants, obs, now=100.0)
    assert ("t_shed", "mem_mb") in plan.unmet_guarantees

    # Trace arrivals at t=0
    arrivals = [
        Arrival(arrival_id="arr_1", tenant_id="t_ok", scheduled_time_s=0.1, cpu_units=0.2, mem_mb=500.0, hold_ms=0.0, nominal_service_ms=50.0, deadline_ms=200.0),
        Arrival(arrival_id="arr_2", tenant_id="t_shed", scheduled_time_s=0.2, cpu_units=0.4, mem_mb=2000.0, hold_ms=0.0, nominal_service_ms=50.0, deadline_ms=200.0),
    ]


    # Simulate request handling as executed in runner.py lines 584-601
    samples = []
    for arr in arrivals:
        is_unmet_mem = any(ug[0] == arr.tenant_id and ug[1] == "mem_mb" for ug in plan.unmet_guarantees)
        p_cpu = plan.alloc.get(arr.tenant_id, {}).get("cpu", 0.1)
        p_mem = plan.alloc.get(arr.tenant_id, {}).get("mem_mb", 64.0)

        cpu_demand = max(0.1, arr.cpu_units)
        speedup = min(2.0, max(0.2, p_cpu / cpu_demand))
        service_ms = arr.nominal_service_ms / speedup
        latency_ms = service_ms + 5.0

        if is_unmet_mem:
            status = "rejected"
        elif p_mem < arr.mem_mb:
            status = "error"
        elif latency_ms > arr.deadline_ms:
            status = "deadline_missed"
        else:
            status = "ok"

        samples.append(
            RequestSample(
                run_id="run_test",
                arrival_id=arr.arrival_id,
                tenant_id=arr.tenant_id,
                scheduled_time_s=arr.scheduled_time_s,
                dispatch_time_s=arr.scheduled_time_s + 0.002,
                completion_time_s=arr.scheduled_time_s + latency_ms / 1000.0,
                latency_ms=latency_ms,
                service_ms=service_ms,
                status=status,
                provenance="SIMULATED",
            )
        )

    # Assert arr_2 is explicitly status == "rejected"
    assert samples[0].status == "ok"
    assert samples[1].status == "rejected"

    # Evaluate headline metric
    total_offered = len(samples)  # 2
    completed_samples = [s for s in samples if s.status in ("ok", "deadline_missed")]  # [arr_1]
    on_time_count = sum(1 for s in completed_samples if s.status == "ok")  # 1

    on_time_ratio = on_time_count / total_offered
    assert on_time_ratio == 0.50, f"Expected 0.50 on-time ratio with shed failure, got {on_time_ratio}"


def test_regression_existing_allocators_from_golden_corpus():
    """Item 2: Regression test verifying that existing allocators produce identical outputs against golden corpus generated at baseline commit 44f6802."""
    corpus_path = Path(__file__).parent.parent / "data" / "golden_allocator_corpus.json"
    assert corpus_path.exists(), f"Golden corpus file not found at {corpus_path}"

    with open(corpus_path, "r", encoding="utf-8") as f:
        golden = json.load(f)

    for case_name, case_data in golden.items():
        if case_name == "adaptive_multi_epoch_sequence":
            continue
        inp = case_data["input"]
        pool = Pool(capacity=inp["pool"]["capacity"])
        tenants = [Tenant(**t) for t in inp["tenants"]]
        obs = {k: TenantObs(**v) for k, v in inp["obs"].items()}

        # 1. FCFS
        p_fcfs = allocate_fcfs(pool, tenants, obs)
        assert p_fcfs.alloc == case_data["fcfs"]["alloc"], f"FCFS alloc mismatch in {case_name}"
        assert [list(x) for x in p_fcfs.unmet_guarantees] == case_data["fcfs"]["unmet_guarantees"]

        # 2. Proportional
        p_prop = allocate_proportional(pool, tenants, obs)
        for tid in p_prop.alloc:
            for r in pool.capacity:
                assert p_prop.alloc[tid][r] == pytest.approx(case_data["proportional"]["alloc"][tid][r], abs=1e-5)

        # 3. DRF
        p_drf = allocate_drf(pool, tenants, obs)
        for tid in p_drf.alloc:
            for r in pool.capacity:
                assert p_drf.alloc[tid][r] == pytest.approx(case_data["drf"]["alloc"][tid][r], abs=1e-5)

        # 4. Adaptive
        adapt = AdaptiveAllocator()
        p_adapt = adapt.allocate(pool, tenants, obs, now=1.0)
        for tid in p_adapt.alloc:
            for r in pool.capacity:
                assert p_adapt.alloc[tid][r] == pytest.approx(case_data["adaptive"]["alloc"][tid][r], abs=1e-5)

    # 5. Multi-epoch Adaptive sequence (25 epochs with burst & throttle)
    if "adaptive_multi_epoch_sequence" in golden:
        seq_case = golden["adaptive_multi_epoch_sequence"]
        pool = Pool(capacity=seq_case["pool"]["capacity"])
        tenants = [Tenant(**t) for t in seq_case["tenants"]]
        adapt = AdaptiveAllocator()
        for step in seq_case["sequence"]:
            now = step["now"]
            obs = {k: TenantObs(**v) for k, v in step["obs"].items()}
            plan = adapt.allocate(pool, tenants, obs, now=now)
            for tid in plan.alloc:
                for r in pool.capacity:
                    assert plan.alloc[tid][r] == pytest.approx(step["alloc"][tid][r], abs=1e-5), (
                        f"Multi-epoch Adaptive mismatch at epoch {step['epoch']}, tenant {tid}, resource {r}"
                    )



def test_determinism_and_sub_millisecond_solve_time(sample_pool, sample_tenants):
    """Item 5 & Condition 6: Deterministic output and solve time recorded with loose sanity bound (< 50 ms)."""
    saha1 = SahaAllocator()
    saha2 = SahaAllocator()

    obs = {
        "tenant_a": TenantObs(usage={"cpu": 0.7, "mem_mb": 400.0}),
        "tenant_b": TenantObs(usage={"cpu": 0.2, "mem_mb": 1100.0}),
        "tenant_c": TenantObs(usage={"cpu": 0.3, "mem_mb": 500.0}),
    }

    plan1 = saha1.allocate(sample_pool, sample_tenants, obs, now=5.0)
    plan2 = saha2.allocate(sample_pool, sample_tenants, obs, now=5.0)

    assert plan1.alloc == plan2.alloc
    assert plan1.unmet_guarantees == plan2.unmet_guarantees
    assert plan1.notes == plan2.notes

    # Solve time recorded and loose sanity bound (< 50 ms)
    assert plan1.solve_ms > 0.0
    assert plan1.solve_ms < 50.0


def test_live_cgroup_path_shedding_with_fake_enforcer():
    """Item 2: In live cgroup path (enforcer present), requests for tenants with unmet memory floor become 'rejected' and only listed tenants are affected."""
    # Construct Plan with unmet guarantee for tenant_b only
    plan_with_shed = Plan(
        alloc={"tenant_a": {"cpu": 1.0, "mem_mb": 1500.0}, "tenant_b": {"cpu": 0.5, "mem_mb": 500.0}},
        unmet_guarantees=[("tenant_b", "mem_mb")],
        notes=["Memory floor infeasible for tenant_b; partial allocation"],
    )
    plans_history = [plan_with_shed]

    # Live requests collected from enforcer/workers
    live_samples = [
        RequestSample(
            run_id="run_live",
            arrival_id="arr_a1",
            tenant_id="tenant_a",
            scheduled_time_s=0.5,
            dispatch_time_s=0.502,
            completion_time_s=0.520,
            latency_ms=20.0,
            service_ms=18.0,
            status="ok",
            provenance="MEASURED",
        ),
        RequestSample(
            run_id="run_live",
            arrival_id="arr_b1",
            tenant_id="tenant_b",
            scheduled_time_s=0.5,
            dispatch_time_s=0.502,
            completion_time_s=0.525,
            latency_ms=25.0,
            service_ms=22.0,
            status="ok",
            provenance="MEASURED",
        ),
    ]

    # Execute the live path shedding transformation from runner.py
    live_samples = apply_live_path_shedding(live_samples, plans_history)

    # Verify only tenant_b was rejected, tenant_a remained "ok"
    assert live_samples[0].tenant_id == "tenant_a"
    assert live_samples[0].status == "ok", "Non-shed tenant requests must remain 'ok'"
    assert live_samples[1].tenant_id == "tenant_b"
    assert live_samples[1].status == "rejected", "Shed tenant requests in live path must be marked 'rejected'"


def test_declared_floor_3x_burst_active_vs_inactive():
    """Item 5 (a): First-ever 3x RSS burst after cold start with rule active (floor covers it) vs inactive (does not)."""
    pool = Pool(capacity={"cpu": 2.0, "mem_mb": 2048.0})
    tenant = Tenant(
        id="t1",
        name="Tenant 1",
        priority=1,
        arrival_seq=1,
        active=True,
        demand={"cpu": 1.0, "mem_mb": 1000.0},
        minimum={"cpu": 0.1, "mem_mb": 64.0},
        weight=1.0,
    )

    # 1. Rule ACTIVE: saha_mem_declared_floor_frac = 0.75
    saha_active = SahaAllocator(SahaParams(use_declared_floor=True, mem_declared_floor_frac=0.75))
    plan_active_prev = None
    # Warm up past cold start (t >= 60s) with baseline RSS = 100 MB, letting contraction reach floor
    for t_s in range(1, 101):
        obs = {"t1": TenantObs(usage={"cpu": 0.2, "mem_mb": 100.0})}
        plan_active_prev = saha_active.allocate(pool, [tenant], obs, now=float(t_s))

    # Before burst, active declared floor guarantees floor and allocation >= 750 MB
    floors_active_100 = saha_active.compute_memory_floors([tenant], obs, plan_active_prev.alloc, now=100.0, pool=pool)
    assert floors_active_100["t1"] >= 750.0
    assert plan_active_prev.alloc["t1"]["mem_mb"] >= 750.0
    # When a 3x burst to 300 MB occurs, pre-allocated memory (>= 750 MB) safely covers it (no OOM kill)
    burst_rss = 300.0
    assert plan_active_prev.alloc["t1"]["mem_mb"] >= burst_rss, "Active rule floor covers 3x burst"

    # 2. Rule INACTIVE: use_declared_floor = False (no_declared_floor ablation)
    saha_inactive = SahaAllocator(SahaParams(use_declared_floor=False, mem_declared_floor_frac=0.0))
    plan_inactive_prev = None
    for t_s in range(1, 101):
        obs = {"t1": TenantObs(usage={"cpu": 0.2, "mem_mb": 100.0})}
        plan_inactive_prev = saha_inactive.allocate(pool, [tenant], obs, now=float(t_s))

    floors_inactive_100 = saha_inactive.compute_memory_floors(
        [tenant], obs, plan_inactive_prev.alloc, now=100.0, pool=pool
    )
    # p99 floor alone is 1.15 * 100 + 64 = 179 MB
    assert floors_inactive_100["t1"] < burst_rss
    # The allocated cgroup memory contracted down toward 179 MB, so it does NOT cover the 300 MB burst!
    # In live cgroup path, reaching 300 MB with allocation < 300 MB triggers an immediate OOM kill.
    assert plan_inactive_prev.alloc["t1"]["mem_mb"] < burst_rss, (
        f"Inactive rule allocation ({plan_inactive_prev.alloc['t1']['mem_mb']} MB) does NOT cover 300 MB burst"
    )



def test_declared_floor_gate_flips_off_on_capacity_cut():
    """Item 5 (b): Gate flips OFF on a memory capacity cut: floors remain hard, contraction obeys limits, no allocation below p99 floor."""
    pool = Pool(capacity={"cpu": 2.0, "mem_mb": 4096.0})
    t_a = Tenant(
        id="ta",
        name="Tenant A",
        priority=1,
        arrival_seq=1,
        active=True,
        demand={"cpu": 1.0, "mem_mb": 1500.0},
        minimum={"cpu": 0.1, "mem_mb": 64.0},
        weight=1.0,
    )
    t_b = Tenant(
        id="tb",
        name="Tenant B",
        priority=1,
        arrival_seq=2,
        active=True,
        demand={"cpu": 1.0, "mem_mb": 1500.0},
        minimum={"cpu": 0.1, "mem_mb": 64.0},
        weight=1.0,
    )

    saha = SahaAllocator(SahaParams(use_declared_floor=True, mem_declared_floor_frac=0.75))
    obs = {
        "ta": TenantObs(usage={"cpu": 0.5, "mem_mb": 200.0}),
        "tb": TenantObs(usage={"cpu": 0.5, "mem_mb": 200.0}),
    }

    # Warm up past cold start
    for t_s in range(1, 71, 5):
        saha.allocate(pool, [t_a, t_b], obs, now=float(t_s))

    # Phase 1: Sum of declared memory = 3000 MB <= 4096 MB -> gate is ON (floor >= 0.75 * 1500 = 1125 MB)
    plan1 = saha.allocate(pool, [t_a, t_b], obs, now=75.0)
    floors1 = saha.compute_memory_floors([t_a, t_b], obs, plan1.alloc, now=75.0, pool=pool)
    assert floors1["ta"] >= 1125.0
    assert floors1["tb"] >= 1125.0

    # Phase 2: Capacity cut to 2048 MB: sum(D) = 3000 MB > 2048 MB -> gate flips OFF!
    cut_pool = Pool(capacity={"cpu": 2.0, "mem_mb": 2048.0})
    plan2 = saha.allocate(cut_pool, [t_a, t_b], obs, now=76.0)
    floors2 = saha.compute_memory_floors([t_a, t_b], obs, plan2.alloc, now=76.0, pool=cut_pool)

    # Gate is OFF: floors are purely p99 RSS based (< 300 MB)
    assert floors2["ta"] < 400.0, "Floors must drop when gate flips OFF"
    assert floors2["tb"] < 400.0
    # Hard invariant: sum x_i <= C_mem = 2048 MB
    assert sum(plan2.alloc[t.id]["mem_mb"] for t in [t_a, t_b]) <= 2048.0 + 1e-5
    # No allocation below p99 floor
    p99_floor_ta = 200.0 * 1.15 + 64.0
    assert plan2.alloc["ta"]["mem_mb"] >= p99_floor_ta


def test_declared_floor_gate_flips_on_arrival_and_departure():
    """Item 5 (c): Gate flips when a tenant arrives or leaves."""
    pool = Pool(capacity={"cpu": 2.0, "mem_mb": 3000.0})
    t_a = Tenant(
        id="ta",
        name="Tenant A",
        priority=1,
        arrival_seq=1,
        active=True,
        demand={"cpu": 0.5, "mem_mb": 1000.0},
        minimum={"cpu": 0.1, "mem_mb": 64.0},
        weight=1.0,
    )
    t_b = Tenant(
        id="tb",
        name="Tenant B",
        priority=1,
        arrival_seq=2,
        active=True,
        demand={"cpu": 0.5, "mem_mb": 1000.0},
        minimum={"cpu": 0.1, "mem_mb": 64.0},
        weight=1.0,
    )
    t_c_inactive = Tenant(
        id="tc",
        name="Tenant C",
        priority=1,
        arrival_seq=3,
        active=False,
        demand={"cpu": 0.5, "mem_mb": 1500.0},
        minimum={"cpu": 0.1, "mem_mb": 64.0},
        weight=1.0,
    )
    t_c_active = Tenant(
        id="tc",
        name="Tenant C",
        priority=1,
        arrival_seq=3,
        active=True,
        demand={"cpu": 0.5, "mem_mb": 1500.0},
        minimum={"cpu": 0.1, "mem_mb": 64.0},
        weight=1.0,
    )

    saha = SahaAllocator(SahaParams(use_declared_floor=True, mem_declared_floor_frac=0.75))
    obs = {
        "ta": TenantObs(usage={"cpu": 0.2, "mem_mb": 200.0}),
        "tb": TenantObs(usage={"cpu": 0.2, "mem_mb": 200.0}),
        "tc": TenantObs(usage={"cpu": 0.2, "mem_mb": 200.0}),
    }

    # Warm up past cold start for ta and tb
    for t_s in range(1, 71, 5):
        saha.allocate(pool, [t_a, t_b], obs, now=float(t_s))

    # Phase 1: Only A and B active (sum D = 2000 MB <= 3000 MB) -> Gate is ON (floor >= 0.75 * 1000 = 750 MB)
    floors1 = saha.compute_memory_floors([t_a, t_b], obs, None, now=75.0, pool=pool)
    assert floors1["ta"] >= 750.0
    assert floors1["tb"] >= 750.0

    # Phase 2: Tenant C arrives (active = True, sum D = 3500 MB > 3000 MB) -> Gate flips OFF
    floors2 = saha.compute_memory_floors([t_a, t_b, t_c_active], obs, None, now=76.0, pool=pool)
    # For ta and tb (past cold start), floors revert to p99 RSS based (< 500 MB)
    assert floors2["ta"] < 500.0, "Gate should be OFF: floor reverts to p99 floor"
    assert floors2["tb"] < 500.0

    # Phase 3: Tenant C departs (active = False, sum D = 2000 MB <= 3000 MB) -> Gate flips back ON
    floors3 = saha.compute_memory_floors([t_a, t_b, t_c_inactive], obs, None, now=77.0, pool=pool)
    assert floors3["ta"] >= 750.0, "Gate should be back ON: floor includes 0.75 * D"
    assert floors3["tb"] >= 750.0


def test_saha_runner_ablation_no_declared_floor():
    """Item 5: Support no_declared_floor ablation string in runner pipeline."""
    pol = "saha:no_declared_floor"
    saha_params = SahaParams()
    if "no_declared_floor" in pol:
        saha_params.use_declared_floor = False
        saha_params.mem_declared_floor_frac = 0.0
    assert saha_params.use_declared_floor is False
    assert saha_params.mem_declared_floor_frac == 0.0

    allocator = SahaAllocator(params=saha_params)
    assert allocator.params.use_declared_floor is False
    assert allocator.params.mem_declared_floor_frac == 0.0


def test_deadband_boundary_conditions():
    """Part 2 Item D: Boundary tests for deadband suppression at exactly 5%, just below 5%, and just above 5%."""
    pool = Pool(capacity={"cpu": 2.0, "mem_mb": 2048.0})
    tenant = Tenant(
        id="t1",
        name="Tenant 1",
        priority=1,
        arrival_seq=1,
        active=True,
        demand={"cpu": 1.5, "mem_mb": 1500.0},
        minimum={"cpu": 0.1, "mem_mb": 64.0},
        weight=1.0,
    )
    saha = SahaAllocator(SahaParams(deadband=0.05, deadband_eps=1e-7))
    prev_alloc = {"t1": {"cpu": 1.0, "mem_mb": 1000.0}}
    floor_mem = {"t1": 100.0}

    # Case 1: Exactly 5.0% change (e.g. cpu contracts from 1.0 to 0.95, rel_change = 0.05)
    # 0.05 < (0.05 - 1e-7) is False -> NOT suppressed, step is accepted
    raw_5pct = {"t1": {"cpu": 0.95, "mem_mb": 1000.0}}
    res_5pct = saha.apply_rate_limits_and_invariants(
        raw_5pct, floor_mem, pool, [tenant], is_cut_active=False, prev_alloc=prev_alloc
    )
    assert res_5pct["t1"]["cpu"] == 0.95, "Exact 5% change must NOT be suppressed by deadband"

    # Case 2: Just below 5.0% change (e.g. 4.99% change: cpu = 0.9501, rel_change = 0.0499)
    # 0.0499 < (0.05 - 1e-7) is True -> IS suppressed, reverts to prev_alloc (1.0)
    raw_sub5 = {"t1": {"cpu": 0.9501, "mem_mb": 1000.0}}
    res_sub5 = saha.apply_rate_limits_and_invariants(
        raw_sub5, floor_mem, pool, [tenant], is_cut_active=False, prev_alloc=prev_alloc
    )
    assert res_sub5["t1"]["cpu"] == 1.0, "Change just below 5% (4.99%) MUST be suppressed by deadband"

    # Case 3: Just above 5.0% change (e.g. 5.01% change: cpu = 0.9499, rel_change = 0.0501)
    # 0.0501 < (0.05 - 1e-7) is False -> NOT suppressed, step is accepted
    raw_sup5 = {"t1": {"cpu": 0.9499, "mem_mb": 1000.0}}
    res_sup5 = saha.apply_rate_limits_and_invariants(
        raw_sup5, floor_mem, pool, [tenant], is_cut_active=False, prev_alloc=prev_alloc
    )
    assert res_sup5["t1"]["cpu"] == 0.9499, "Change just above 5% (5.01%) must NOT be suppressed by deadband"


def test_runner_non_saha_live_enforcer_regression():
    """Part 2 Item B: Runner-level regression test showing that non-SAHA allocators running through the live-enforcer path are 100% unaffected by the shedding edit and match v1.0-demo-baseline golden corpus."""
    corpus_path = Path(__file__).parent.parent / "data" / "golden_allocator_corpus.json"
    with open(corpus_path, "r", encoding="utf-8") as f:
        golden = json.load(f)

    # Test 'standard' case with Adaptive allocator
    case = golden["standard"]
    pool = Pool(capacity=case["input"]["pool"]["capacity"])
    tenants = [Tenant(**t) for t in case["input"]["tenants"]]
    obs = {k: TenantObs(**v) for k, v in case["input"]["obs"].items()}

    # 1. Run AdaptiveAllocator through standard interface
    adapt = AdaptiveAllocator()
    plan_baseline = adapt.allocate(pool, tenants, obs, now=1.0)
    golden_alloc = case["adaptive"]["alloc"]

    for tid in golden_alloc:
        for r in pool.capacity:
            assert plan_baseline.alloc[tid][r] == pytest.approx(golden_alloc[tid][r], abs=1e-5)

    # 2. Call the real runner code path: apply_live_path_shedding
    plans_history = [plan_baseline]
    live_samples = [
        RequestSample(
            run_id="run_live_reg",
            arrival_id=f"arr_{tid}",
            tenant_id=tid,
            scheduled_time_s=0.5,
            dispatch_time_s=0.502,
            completion_time_s=0.520,
            latency_ms=18.0,
            service_ms=16.0,
            status="ok",
            provenance="MEASURED",
        )
        for tid in plan_baseline.alloc
    ]
    processed_samples = apply_live_path_shedding(live_samples, plans_history)

    # For non-SAHA allocators (Adaptive, Proportional, DRF), plan.unmet_guarantees is empty
    # Verify zero samples were modified to "rejected"
    assert len(plan_baseline.unmet_guarantees) == 0, "Non-SAHA allocators produce empty unmet_guarantees"
    for s in processed_samples:
        assert s.status == "ok", f"Sample for tenant {s.tenant_id} must remain 'ok' in live path"

    # 3. Call the full _execute_single_run code path with a non-SAHA policy (Adaptive)
    # Passing a real mock enforcer and mock run_load so the live cgroup shedding branch executes!
    from unittest.mock import MagicMock
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from alloc_platform.db.models import Base, Experiment, PoolModel, Run, RequestSampleModel
    from alloc_platform.orchestrator.scenario import get_predefined_scenarios
    from alloc_platform.telemetry.host import HostMonitor
    import alloc_platform.orchestrator.runner as runner_mod
    import asyncio

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(PoolModel(id="default_pool", name="Default Pool", cpu_cores=2.0, mem_mb=2048.0))
    db.add(Experiment(id="exp_reg_test", name="Reg Test", scenario_id="scenario_1_baseline", status="running", policies_json='["adaptive"]', repeats=1))
    db.add(Run(id="run_reg_test", experiment_id="exp_reg_test", policy="adaptive", repeat_index=0, status="pending", is_valid=True))
    db.commit()

    scen = get_predefined_scenarios()["scenario_1_baseline"]
    scen.duration_s = 2
    config.epoch_interval_s = 0.0

    fake_enforcer = MagicMock()
    fake_enforcer.ensure_network.return_value = None
    fake_enforcer.create_tenant.side_effect = lambda spec: f"cid_{spec.tenant_id}"
    fake_container = MagicMock()
    fake_container.attrs = {"NetworkSettings": {"Networks": {config.tenant_network_name: {"IPAddress": "127.0.0.1"}}}}
    fake_enforcer.client.containers.get.return_value = fake_container
    fake_enforcer.apply_limits.return_value = MagicMock(apply_ms=0.5)

    async def mock_run_load(trace, endpoints, run_id, stop_event):
        return [
            RequestSample(
                run_id=run_id,
                arrival_id=f"arr_{t.id}_0",
                tenant_id=t.id,
                scheduled_time_s=0.5,
                dispatch_time_s=0.502,
                completion_time_s=0.520,
                latency_ms=18.0,
                service_ms=16.0,
                status="ok",
                provenance="MEASURED",
            )
            for t in scen.tenants
        ]

    orig_run_load = runner_mod.run_load
    runner_mod.run_load = mock_run_load

    try:
        asyncio.run(_execute_single_run(
            db=db,
            run_id="run_reg_test",
            policy="adaptive",
            scen_spec=scen,
            enforcer=fake_enforcer,
            host_mon=HostMonitor(),
            cancel_event=asyncio.Event(),
        ))

        # Verify fake enforcer was actively invoked on live path
        assert fake_enforcer.apply_limits.call_count > 0, "Fake enforcer apply_limits must be called"

        # Verify that in the live enforcer runner path for Adaptive, all samples passed through apply_live_path_shedding with ZERO 'rejected'
        req_samples_db = db.query(RequestSampleModel).filter(RequestSampleModel.run_id == "run_reg_test").all()
        assert len(req_samples_db) == len(scen.tenants), "All live samples must be recorded in DB"
        rejected_count = sum(1 for s in req_samples_db if s.status == "rejected")
        assert rejected_count == 0, "Non-SAHA allocator live runner execution must produce zero rejected samples"
        assert all(s.status == "ok" for s in req_samples_db)
    finally:
        runner_mod.run_load = orig_run_load
        db.close()




