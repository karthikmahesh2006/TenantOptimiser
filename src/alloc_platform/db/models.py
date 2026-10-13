"""SQLAlchemy 2.0 ORM models for Alloc Platform."""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, relationship

SCHEMA_VERSION = 1
METRIC_DEFINITION_VERSION = 1


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    password_hash = Column(String(256), nullable=False)
    role = Column(String(32), default="viewer", nullable=False)  # "admin" or "viewer"
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class PoolModel(Base):
    __tablename__ = "pools"

    id = Column(String(64), primary_key=True)
    name = Column(String(128), nullable=False)
    cpu_cores = Column(Float, nullable=False)
    mem_mb = Column(Float, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class TenantModel(Base):
    __tablename__ = "tenants"

    id = Column(String(64), primary_key=True)
    name = Column(String(128), nullable=False)
    weight = Column(Float, default=1.0, nullable=False)
    priority = Column(Integer, default=1, nullable=False)
    arrival_seq = Column(Integer, default=0, nullable=False)
    active = Column(Boolean, default=True, nullable=False)
    demand_cpu = Column(Float, nullable=False)
    demand_mem_mb = Column(Float, nullable=False)
    min_cpu = Column(Float, default=0.0, nullable=False)
    min_mem_mb = Column(Float, default=0.0, nullable=False)
    workload_type = Column(String(32), default="mixed", nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class ScenarioModel(Base):
    __tablename__ = "scenarios"

    id = Column(String(64), primary_key=True)
    name = Column(String(128), nullable=False)
    duration_s = Column(Integer, default=60, nullable=False)
    seed = Column(Integer, default=42, nullable=False)
    pool_id = Column(String(64), ForeignKey("pools.id"), nullable=False)
    spec_json = Column(Text, nullable=False)  # JSON defining tenants and scheduled events
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class EnvironmentModel(Base):
    __tablename__ = "environments"

    id = Column(String(64), primary_key=True)
    instance_type = Column(String(64), nullable=False)
    kernel_version = Column(String(128), nullable=False)
    docker_version = Column(String(64), nullable=False)
    cgroup_version = Column(String(32), default="v2", nullable=False)
    git_sha = Column(String(64), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Experiment(Base):
    __tablename__ = "experiments"

    id = Column(String(64), primary_key=True)
    name = Column(String(128), nullable=False)
    status = Column(
        String(32), default="pending", nullable=False
    )  # pending, running, completed, failed, cancelled
    scenario_id = Column(String(64), ForeignKey("scenarios.id"), nullable=False)
    env_id = Column(String(64), ForeignKey("environments.id"), nullable=True)
    policies_json = Column(Text, nullable=False)  # e.g. ["fcfs", "proportional", "drf", "adaptive"]
    repeats = Column(Integer, default=1, nullable=False)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at = Column(DateTime, nullable=True)

    runs = relationship("Run", back_populates="experiment", cascade="all, delete-orphan")


class Run(Base):
    __tablename__ = "runs"

    id = Column(String(64), primary_key=True)
    experiment_id = Column(String(64), ForeignKey("experiments.id"), nullable=False)
    policy = Column(String(32), nullable=False)
    repeat_index = Column(Integer, default=0, nullable=False)
    status = Column(String(32), default="pending", nullable=False)
    is_valid = Column(Boolean, default=True, nullable=False)
    invalidation_reason = Column(String(256), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at = Column(DateTime, nullable=True)

    experiment = relationship("Experiment", back_populates="runs")
    metrics = relationship("RunMetric", back_populates="run", cascade="all, delete-orphan")


class AllocationEvent(Base):
    __tablename__ = "allocation_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(64), ForeignKey("runs.id"), nullable=False, index=True)
    epoch = Column(Integer, nullable=False)
    timestamp_s = Column(Float, nullable=False)
    tenant_id = Column(String(64), nullable=False)
    alloc_cpu = Column(Float, nullable=False)
    alloc_mem_mb = Column(Float, nullable=False)
    solve_ms = Column(Float, default=0.0, nullable=False)
    apply_ms = Column(Float, default=0.0, nullable=False)
    provenance = Column(String(32), default="CALCULATED", nullable=False)

    __table_args__ = (Index("ix_alloc_events_run_epoch", "run_id", "epoch"),)


class TenantSample(Base):
    __tablename__ = "tenant_samples"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(64), ForeignKey("runs.id"), nullable=False)
    timestamp_s = Column(Float, nullable=False)
    tenant_id = Column(String(64), nullable=False)
    cpu_cores_used = Column(Float, nullable=False)
    mem_used_mb = Column(Float, nullable=False)
    throttle_ratio = Column(Float, default=0.0, nullable=False)
    throttled_usec = Column(Float, default=0.0, nullable=False)
    nr_throttled = Column(Integer, default=0, nullable=False)
    provenance = Column(String(32), default="MEASURED", nullable=False)

    __table_args__ = (Index("ix_tenant_samples_run_t", "run_id", "timestamp_s"),)


class HostSample(Base):
    __tablename__ = "host_samples"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(64), ForeignKey("runs.id"), nullable=False)
    timestamp_s = Column(Float, nullable=False)
    host_cpu_pct = Column(Float, nullable=False)
    host_mem_used_mb = Column(Float, nullable=False)
    host_mem_total_mb = Column(Float, nullable=False)
    steal_pct = Column(Float, default=0.0, nullable=False)
    cpu_credit_balance = Column(Float, nullable=True)
    provenance = Column(String(32), default="MEASURED", nullable=False)

    __table_args__ = (Index("ix_host_samples_run_t", "run_id", "timestamp_s"),)


class RequestSampleModel(Base):
    __tablename__ = "request_samples"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(64), ForeignKey("runs.id"), nullable=False)
    arrival_id = Column(String(64), nullable=False)
    tenant_id = Column(String(64), nullable=False)
    scheduled_time_s = Column(Float, nullable=False)
    dispatch_time_s = Column(Float, nullable=False)
    completion_time_s = Column(Float, nullable=False)
    latency_ms = Column(Float, nullable=False)
    service_ms = Column(Float, nullable=False)
    status = Column(String(32), nullable=False)  # ok, deadline_missed, rejected, error
    provenance = Column(String(32), default="MEASURED", nullable=False)

    __table_args__ = (Index("ix_request_samples_run_t", "run_id", "scheduled_time_s"),)


class RunMetric(Base):
    __tablename__ = "run_metrics"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(64), ForeignKey("runs.id"), nullable=False, index=True)
    metric_name = Column(String(64), nullable=False)
    metric_value = Column(Float, nullable=False)
    metric_version = Column(Integer, default=METRIC_DEFINITION_VERSION, nullable=False)
    provenance = Column(String(32), default="CALCULATED", nullable=False)

    run = relationship("Run", back_populates="metrics")


class ExperimentLock(Base):
    __tablename__ = "experiment_locks"

    id = Column(Integer, primary_key=True, default=1)
    experiment_id = Column(String(64), nullable=False)
    acquired_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    action = Column(String(64), nullable=False)
    username = Column(String(64), default="system", nullable=False)
    details = Column(Text, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)
