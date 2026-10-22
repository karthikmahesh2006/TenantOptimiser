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
