"""Direct cgroup v2 reader and verifier."""

import os


class CgroupReader:
    """Reads cgroup v2 metrics and limits for a container."""

    def __init__(self, container_id: str, cgroup_fs_root: str = "/sys/fs/cgroup") -> None:
        self.container_id = container_id
        self.cgroup_fs_root = cgroup_fs_root
        self._cgroup_path: str | None = self._resolve_cgroup_path()

    def _resolve_cgroup_path(self) -> str | None:
        # Typical systemd driver path on Amazon Linux 2023:
        # /sys/fs/cgroup/system.slice/docker-<id>.scope
        long_id = self.container_id
        candidates = [
            os.path.join(self.cgroup_fs_root, "system.slice", f"docker-{long_id}.scope"),
            os.path.join(self.cgroup_fs_root, f"docker-{long_id}.scope"),
            os.path.join(self.cgroup_fs_root, "docker", long_id),
            os.path.join(self.cgroup_fs_root, long_id),
        ]
        for c in candidates:
            if os.path.isdir(c):
                return c
        return None

    def read_limits(self) -> tuple[float | None, float | None]:
        """Reads back (cpu_cores, mem_mb) from cgroup v2 cpu.max and memory.max."""
        if not self._cgroup_path:
            return None, None

        cpu_cores = None
        mem_mb = None

        # 1. cpu.max format: "<quota> <period>" or "max <period>"
        cpu_max_file = os.path.join(self._cgroup_path, "cpu.max")
        if os.path.isfile(cpu_max_file):
            try:
                with open(cpu_max_file, "r") as f:
                    parts = f.read().strip().split()
                    if len(parts) >= 2:
                        quota_str, period_str = parts[0], parts[1]
                        if quota_str != "max":
                            cpu_cores = float(quota_str) / float(period_str)
            except Exception:
                pass

        # 2. memory.max format: "<bytes>" or "max"
        mem_max_file = os.path.join(self._cgroup_path, "memory.max")
        if os.path.isfile(mem_max_file):
            try:
                with open(mem_max_file, "r") as f:
                    val_str = f.read().strip()
                    if val_str != "max":
                        mem_mb = float(val_str) / (1024.0 * 1024.0)
            except Exception:
                pass

        return cpu_cores, mem_mb

    def read_stats(self) -> dict[str, float]:
        """Reads cpu.stat and memory.current from cgroup v2.

        Returns:
            {"usage_usec": float, "nr_throttled": float, "throttled_usec": float, "memory_current_mb": float}
        """
        stats = {
            "usage_usec": 0.0,
            "nr_throttled": 0.0,
            "throttled_usec": 0.0,
            "memory_current_mb": 0.0,
        }
        if not self._cgroup_path:
            return stats

        cpu_stat_file = os.path.join(self._cgroup_path, "cpu.stat")
        if os.path.isfile(cpu_stat_file):
            try:
                with open(cpu_stat_file, "r") as f:
                    for line in f:
                        parts = line.strip().split()
                        if len(parts) == 2:
                            k, v = parts[0], parts[1]
                            if k == "usage_usec":
                                stats["usage_usec"] = float(v)
                            elif k == "nr_throttled":
                                stats["nr_throttled"] = float(v)
                            elif k == "throttled_usec":
                                stats["throttled_usec"] = float(v)
            except Exception:
                pass

        mem_curr_file = os.path.join(self._cgroup_path, "memory.current")
        if os.path.isfile(mem_curr_file):
            try:
                with open(mem_curr_file, "r") as f:
                    bytes_val = float(f.read().strip())
                    stats["memory_current_mb"] = bytes_val / (1024.0 * 1024.0)
            except Exception:
                pass

        return stats
