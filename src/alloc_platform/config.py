"""Global configuration and safety limits for Alloc Platform."""

import os

from pydantic import BaseModel, Field


class PlatformConfig(BaseModel):
    # Safety limits and hard caps
    max_tenants: int = 10
    max_duration_s: int = 600
    max_repeats: int = 10
    max_concurrent_experiments: int = 1

    # Container limits
    min_container_cpu: float = 0.05
    max_container_cpu: float = 4.0
    min_container_mem_mb: float = 32.0
    max_container_mem_mb: float = 16384.0

    # Default pool allocation ratio (relative to detected host capacity)
    default_host_cpu_ratio: float = 0.75
    default_host_mem_ratio: float = 0.60

    # Thresholds for run invalidation
    max_cpu_steal_pct: float = 5.0
    min_cpu_credit_balance: float = 10.0

    # Adaptive Allocator single-source constants
    memory_safety_mb: float = 64.0
    throttle_threshold: float = 0.05

    # Enforcement & Docker
    alloc_mode: str = Field(
        default_factory=lambda: os.getenv("ALLOC_MODE", "auto").lower()
    )
    docker_host: str = Field(
        default_factory=lambda: os.getenv("DOCKER_HOST", "tcp://127.0.0.1:2375")
    )
    tenant_network_name: str = "alloc-tenants"
    worker_image_name: str = "alloc-worker:latest"
    cgroup_fs_root: str = "/sys/fs/cgroup"

    # Persistence & Security
    db_path: str = Field(default_factory=lambda: os.getenv("DB_PATH", "alloc.db"))
    secret_key: str = Field(
        default_factory=lambda: os.getenv(
            "SECRET_KEY", "dev-insecure-secret-key-replace-in-prod-0000000000"
        )
    )
    session_cookie_name: str = "alloc_session"
    session_max_age_s: int = 86400 * 7  # 7 days

    # Timing
    epoch_interval_s: float = 1.0
    sampler_interval_s: float = 1.0


# Default singleton instance
config = PlatformConfig()
