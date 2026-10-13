"""Host system telemetry reader (procfs and CloudWatch metrics)."""

import os
import subprocess
import time
from dataclasses import dataclass


@dataclass
class HostTelemetry:
    """Snapshot of host CPU, memory, and virtualization metrics."""

    cpu_pct: float
    mem_used_mb: float
    mem_total_mb: float
    steal_pct: float
    cpu_credit_balance: float | None = None
    provenance: str = "MEASURED"


class HostMonitor:
    """Reads host hardware state from Linux procfs and optional CloudWatch."""

    def __init__(self) -> None:
        self.prev_cpu_times: tuple[float, float, float] | None = None  # (idle, total, steal)

    def sample_procfs(self) -> tuple[float, float, float, float]:
        """Reads /proc/stat and /proc/meminfo.

        Returns:
            (cpu_pct, mem_used_mb, mem_total_mb, steal_pct)
        """
        cpu_pct = 0.0
        steal_pct = 0.0
        mem_used_mb = 0.0
        mem_total_mb = 4096.0

        # Read /proc/stat
        if os.path.isfile("/proc/stat"):
            try:
                with open("/proc/stat", "r") as f:
                    for line in f:
                        if line.startswith("cpu "):
                            fields = [float(x) for x in line.strip().split()[1:]]
                            # user, nice, system, idle, iowait, irq, softirq, steal, guest, guest_nice
                            idle = fields[3] + (fields[4] if len(fields) > 4 else 0.0)
                            steal = fields[7] if len(fields) > 7 else 0.0
                            total = sum(fields)

                            if self.prev_cpu_times is not None:
                                prev_idle, prev_total, prev_steal = self.prev_cpu_times
                                delta_total = max(1e-6, total - prev_total)
                                delta_idle = max(0.0, idle - prev_idle)
                                delta_steal = max(0.0, steal - prev_steal)

                                cpu_pct = max(
                                    0.0,
                                    min(100.0, ((delta_total - delta_idle) / delta_total) * 100.0),
                                )
                                steal_pct = max(
                                    0.0, min(100.0, (delta_steal / delta_total) * 100.0)
                                )

                            self.prev_cpu_times = (idle, total, steal)
                            break
            except Exception:
                pass

        # Read /proc/meminfo
        if os.path.isfile("/proc/meminfo"):
            try:
                mem_total_kb = 0.0
                mem_avail_kb = 0.0
                with open("/proc/meminfo", "r") as f:
                    for line in f:
                        parts = line.split(":")
                        if len(parts) == 2:
                            k, v = parts[0].strip(), parts[1].strip()
                            if k == "MemTotal":
                                mem_total_kb = float(v.split()[0])
                            elif k == "MemAvailable":
                                mem_avail_kb = float(v.split()[0])
                if mem_total_kb > 0:
                    mem_total_mb = mem_total_kb / 1024.0
                    mem_used_mb = (mem_total_kb - mem_avail_kb) / 1024.0
            except Exception:
                pass

        return round(cpu_pct, 2), round(mem_used_mb, 2), round(mem_total_mb, 2), round(steal_pct, 2)

    def sample_cloudwatch_credits(self, instance_id: str | None = None) -> float | None:
        """Queries CloudWatch CPUCreditBalance if on a burstable instance."""
        if not instance_id:
            return None
        try:
            # Using aws CLI query with 5m window
            cmd = [
                "aws",
                "cloudwatch",
                "get-metric-statistics",
                "--namespace",
                "AWS/EC2",
                "--metric-name",
                "CPUCreditBalance",
                "--dimensions",
                f"Name=InstanceId,Value={instance_id}",
                "--start-time",
                str(int(time.time() - 600)),
                "--end-time",
                str(int(time.time())),
                "--period",
                "300",
                "--statistics",
                "Average",
                "--query",
                "Datapoints[0].Average",
                "--output",
                "text",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if res.returncode == 0 and res.stdout.strip() and res.stdout.strip() != "None":
                return float(res.stdout.strip())
        except Exception:
            pass
        return None
