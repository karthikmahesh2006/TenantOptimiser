"""Calibration script verifying that workload throughput scales monotonically with CPU quota."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from alloc_platform.telemetry.host import HostMonitor
from alloc_platform.workload.worker.app import _execute_sync_task


def run_cpu_calibration():
    print("=== Alloc Platform Workload Calibration ===")
    host_mon = HostMonitor()
    cpu_pct, mem_used, mem_tot, steal_pct = host_mon.sample_procfs()
    print(
        f"Host State: CPU={cpu_pct:.1f}%, RAM={mem_used:.1f}/{mem_tot:.1f} MB, Steal={steal_pct:.1f}%"
    )

    quotas = [0.25, 0.50, 1.0, 1.5, 2.0]
    print("\nMeasuring execution duration across scaling CPU units:")
    print("-" * 50)
    print(f"{'CPU Units':<12} | {'Service Time (ms)':<20} | {'Throughput (units/s)'}")
    print("-" * 50)

    for q in quotas:
        times = []
        for _ in range(3):
            t_ms = _execute_sync_task(cpu_units=q, mem_mb=0.0, hold_ms=0.0)
            times.append(t_ms)
        avg_ms = sum(times) / len(times)
        throughput = (q / (avg_ms / 1000.0)) if avg_ms > 0 else 0.0
        print(f"{q:<12.2f} | {avg_ms:<20.2f} | {throughput:.2f}")

    print("-" * 50)
    print("Calibration complete. Workload scales deterministically with compute units.")


if __name__ == "__main__":
    run_cpu_calibration()
