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
    metric_name: str
    metric_value: float
    provenance: str


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
    metrics: Dict[str, float] = {}


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
