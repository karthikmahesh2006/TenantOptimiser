"""Open-loop Poisson trace generation with scripted bursts and arrival windows."""

from dataclasses import dataclass
from typing import Any

import numpy as np


@dataclass(frozen=True)
class Arrival:
    """Represents a scheduled workload request in an open-loop trace."""

    arrival_id: str
    tenant_id: str
    scheduled_time_s: float
    cpu_units: float
    mem_mb: float
    hold_ms: float
    nominal_service_ms: float
    deadline_ms: float


def build_trace(
    tenants: list[Any],
    duration_s: int,
    seed: int = 42,
    base_rate_rps: float = 2.0,
    burst_multiplier: float = 2.5,
    burst_windows: dict[str, list[tuple]] | None = None,
    tenant_windows: dict[str, tuple] | None = None,
    deadline_factor: float = 5.0,
) -> list[Arrival]:
    """Generates an open-loop trace with seeded Poisson arrivals.

    Every policy evaluates against the exact same Arrival sequence for reproducibility.
    """
    rng = np.random.default_rng(seed)
    arrivals: list[Arrival] = []
    burst_windows = burst_windows or {}
    tenant_windows = tenant_windows or {}

    for t in tenants:
        tid = getattr(t, "id", str(t))
        wtype = getattr(t, "workload_type", "mixed")
        t_start, t_end = tenant_windows.get(tid, (0.0, float(duration_s)))
        t_bursts = burst_windows.get(tid, [])

        curr_t = t_start
        req_counter = 0

        while curr_t < t_end:
            # Check if inside an active burst window
            in_burst = any(b_start <= curr_t <= b_end for b_start, b_end in t_bursts)
            lambda_rate = base_rate_rps * (burst_multiplier if in_burst else 1.0)

            # Exponential inter-arrival interval
            delta_t = rng.exponential(1.0 / max(0.1, lambda_rate))
            curr_t += delta_t
            if curr_t >= t_end:
                break

            req_counter += 1
            # Profile parameters
            if wtype == "cpu_heavy":
                cpu_u = 1.0 + 0.2 * rng.uniform(-1.0, 1.0)
                mem_m = 16.0
                hold = 0.0
                nominal_ms = 40.0
            elif wtype == "memory_heavy":
                cpu_u = 0.1
                mem_m = 128.0 + 16.0 * rng.uniform(-1.0, 1.0)
                hold = 50.0
                nominal_ms = 60.0
            else:  # mixed
                cpu_u = 0.5 + 0.1 * rng.uniform(-1.0, 1.0)
                mem_m = 64.0 + 10.0 * rng.uniform(-1.0, 1.0)
                hold = 25.0
                nominal_ms = 45.0

            deadline_ms = nominal_ms * deadline_factor
            arrivals.append(
                Arrival(
                    arrival_id=f"{tid}_{req_counter}",
                    tenant_id=tid,
                    scheduled_time_s=round(curr_t, 4),
                    cpu_units=round(max(0.05, cpu_u), 3),
                    mem_mb=round(max(4.0, mem_m), 1),
                    hold_ms=round(max(0.0, hold), 1),
                    nominal_service_ms=round(nominal_ms, 2),
                    deadline_ms=round(deadline_ms, 2),
                )
            )

    # Sort strictly by scheduled time
    arrivals.sort(key=lambda a: a.scheduled_time_s)
    return arrivals
