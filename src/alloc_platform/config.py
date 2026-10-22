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

    # Host capacity and pool budget ratios (pool budget <= 75% of host by default)
    host_vcpus: float = 2.0
    host_mem_mb: float = 4096.0
    default_host_cpu_ratio: float = 0.75
    default_host_mem_ratio: float = 0.75

    @property
    def default_pool_cpu(self) -> float:
        return round(self.host_vcpus * self.default_host_cpu_ratio, 2)

    @property
    def default_pool_mem_mb(self) -> float:
        return round(self.host_mem_mb * self.default_host_mem_ratio, 1)

    # Thresholds for run invalidation
    max_cpu_steal_pct: float = 5.0
    min_cpu_credit_balance: float = 10.0

    # Adaptive Allocator single-source constants
    memory_safety_mb: float = 64.0
    throttle_threshold: float = 0.05
    max_shrink_per_epoch: float = 0.20
    deadband: float = 0.05
    min_dwell_s: float = 3.0
    headroom_h: float = 0.20
    ewma_alpha: float = 0.3
    lambda_churn: float = 0.10

    # SAHA (Saturation-Aware Hybrid Allocator) hyperparameters
    # Tunable parameters for Stage 6 calibration (exactly 12 tunables):
    saha_ewma_alpha: float = 0.30
    saha_headroom_h: float = 0.20
    saha_throttle_step: float = 1.25
    saha_rho_low: float = 0.85
    saha_rho_high: float = 1.00
    saha_mem_floor_mult: float = 1.15
    saha_mem_margin_mb: float = 64.0
    saha_mem_margin_frac: float = 0.10
    saha_mem_window_s: float = 60.0
    saha_mem_contract_step: float = 0.05
    saha_cpu_contract_rate: float = 0.20
    saha_deadband: float = 0.05

    # SAHA Structural parameters (fixed, not tuned):
    saha_reclaim_min_share_ratio: float = 0.0
    saha_mem_min_samples: int = 10
    saha_cut_latch_epochs: int = 3
    saha_dhat_min_epsilon: float = 1e-4
    saha_cap_cut_tol: float = 1e-3
    saha_mem_declared_floor_frac: float = 0.75
    saha_deadband_eps: float = 1e-7



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
    data_dir: str = Field(
        default_factory=lambda: os.getenv(
            "DATA_DIR",
            os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data")),
        )
    )
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
