"""Pydantic v2 validation models and schemas for Alloc Platform."""

from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class PoolCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(..., pattern=r"^[a-zA-Z0-9_\-]+$")
    name: str = Field(..., min_length=1, max_length=128)
    cpu_cores: float = Field(..., ge=0.1, le=64.0)
    mem_mb: float = Field(..., ge=64.0, le=262144.0)


class PoolOut(PoolCreate):
    created_at: datetime


class TenantCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(..., pattern=r"^[a-zA-Z0-9_\-]+$")
    name: str = Field(..., min_length=1, max_length=128)
    weight: float = Field(default=1.0, gt=0.0, le=100.0)
    priority: int = Field(default=1, ge=1, le=10)
    arrival_seq: int = Field(default=0, ge=0)
    active: bool = True
    demand_cpu: float = Field(..., ge=0.05, le=16.0)
    demand_mem_mb: float = Field(..., ge=32.0, le=32768.0)
    min_cpu: float = Field(default=0.0, ge=0.0, le=16.0)
    min_mem_mb: float = Field(default=0.0, ge=0.0, le=32768.0)
    workload_type: str = Field(default="mixed", pattern=r"^(cpu_heavy|memory_heavy|mixed)$")


class TenantUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = None
    weight: Optional[float] = Field(default=None, gt=0.0, le=100.0)
    priority: Optional[int] = Field(default=None, ge=1, le=10)
    arrival_seq: Optional[int] = Field(default=None, ge=0)
    active: Optional[bool] = None
    demand_cpu: Optional[float] = Field(default=None, ge=0.05, le=16.0)
    demand_mem_mb: Optional[float] = Field(default=None, ge=32.0, le=32768.0)
    min_cpu: Optional[float] = Field(default=None, ge=0.0, le=16.0)
    min_mem_mb: Optional[float] = Field(default=None, ge=0.0, le=32768.0)
    workload_type: Optional[str] = Field(default=None, pattern=r"^(cpu_heavy|memory_heavy|mixed)$")


class TenantOut(TenantCreate):
    created_at: datetime


class ExperimentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., min_length=1, max_length=128)
    scenario_id: str
    policies: List[str] = Field(default=["fcfs", "proportional", "drf", "adaptive"])
    repeats: int = Field(default=1, ge=1, le=10)


class RunMetricOut(BaseModel):
    """Single metric record with explicit provenance label and documented definitions.

    Key Metric Definitions:
        - on_time_ratio (MEASURED): Requests completed within deadline divided by total requests offered.
          Counts rejected and censored requests as failures. Headline unmet-need efficiency metric.
        - mean_satisfaction (CALCULATED): Bottleneck ratio min_r(alloc_r / demand_r).
          The denominator is strictly contractual DECLARED demand (SLA), NOT dynamic estimate.
        - unmet_demand_cpu/mem (CALCULATED): Demand minus allocation evaluated against DECLARED demand.
        - throttle_time_ratio (MEASURED): Fraction of execution time throttled by cgroup v2 cpu.stat.
          Unmet actual need component 1: throttled_usec / (throttled_usec + usage_usec).
        - deadline_miss_rate (MEASURED): Fraction of completed requests missing latency deadline.
          Unmet actual need component 2: deadline_misses / completed.
        - excluded_count (MEASURED): Total requests rejected or censored (timeouts).
        - idle_reserved_cpu/mem_mb (CALCULATED): Allocated quota that remained idle (alloc - consumed).
        - non_tenant_cpu_cores_avg (MEASURED): Host CPU cores consumed by API, loadgen, and telemetry.
          Run invalidated if exceeding 25% host reservation (0.50 vCPU).
        - solve_ms_avg / solve_time_epoch_pct (MEASURED): Absolute solver latency (ms) and fraction of epoch.
    """

    metric_name: str = Field(..., description="Unique metric identifier, e.g. mean_satisfaction or throttle_time_ratio")
    metric_value: float = Field(..., description="Numerical value of the metric")
    provenance: str = Field(..., description="Provenance label: MEASURED, CALCULATED, CONFIGURED, or SIMULATED")


class RunOut(BaseModel):
    id: str
    experiment_id: str
    policy: str
    repeat_index: int
    status: str
    is_valid: bool
    invalidation_reason: Optional[str] = None
    created_at: datetime
    completed_at: Optional[datetime] = None
    metrics: Dict[str, float] = Field(
        default={},
        description=(
            "Summary metrics dictionary. Satisfaction and unmet demand evaluated against DECLARED demand. "
            "Unmet actual need reported via separate throttle_time_ratio and deadline_miss_rate."
        ),
    )


class ExperimentOut(BaseModel):
    id: str
    name: str
    status: str
    scenario_id: str
    policies: List[str]
    repeats: int
    created_at: datetime
    completed_at: Optional[datetime] = None
    runs: List[RunOut] = []


class StatusResponse(BaseModel):
    status: str
    system_time: str
    active_experiments: int
    docker_available: bool
    cgroup_version: str
    git_sha: str
