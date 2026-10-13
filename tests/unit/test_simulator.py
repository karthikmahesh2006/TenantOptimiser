"""Unit tests for offline fluid simulator."""

from alloc_platform.allocator.simulator import SimScenario, simulate
from alloc_platform.allocator.types import Pool, Tenant


def test_simulator_provenance_banner():
    """Verify Rule A1: All simulator outputs are strictly tagged SIMULATED."""
    pool = Pool(capacity={"cpu": 4.0, "mem_mb": 4096.0})
    tenants = [
        Tenant(
            id="t1",
            name="T1",
            weight=1.0,
            priority=1,
            arrival_seq=1,
            active=True,
            demand={"cpu": 2.0, "mem_mb": 2048.0},
            minimum={"cpu": 0.2, "mem_mb": 256.0},
        ),
        Tenant(
            id="t2",
            name="T2",
            weight=1.0,
            priority=1,
            arrival_seq=2,
            active=True,
            demand={"cpu": 2.0, "mem_mb": 2048.0},
            minimum={"cpu": 0.2, "mem_mb": 256.0},
        ),
    ]
    scenario = SimScenario(name="test_sim", duration_s=5, pool=pool, tenants=tenants)

    for pol in ["fcfs", "proportional", "drf", "adaptive"]:
        res = simulate(pol, scenario, seed=42)
        assert res.provenance == "SIMULATED"
        assert len(res.epochs) == 5
        assert len(res.plans) == 5
        assert "jain_fairness_primary" in res.metrics
