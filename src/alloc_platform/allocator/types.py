"""Data structures and types for the resource allocator."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Tenant:
    """Represents a tenant contending for compute and memory resources.

    Attributes:
        id: Unique identifier for the tenant.
        name: Human-readable display name.
        weight: Resource weight (positive float), used for weighted allocations.
        priority: Integer priority (higher values indicate higher priority).
        arrival_seq: Sequence number tracking order of arrival for tie-breaking/FCFS.
        active: Whether the tenant is currently active and eligible for allocation.
        demand: Declared resource requirement, e.g. {'cpu': 1.0, 'mem_mb': 512.0}.
        minimum: Guaranteed minimum resource requirement, e.g. {'cpu': 0.2, 'mem_mb': 128.0}.
        workload_type: Workload profile ('cpu_heavy', 'memory_heavy', 'mixed', etc.).
    """

    id: str
    name: str
    weight: float
    priority: int
    arrival_seq: int
    active: bool
    demand: dict[str, float]
    minimum: dict[str, float]
    workload_type: str = "mixed"


@dataclass(frozen=True)
class Pool:
    """Resource pool representing allocatable host capacity.

    Attributes:
        capacity: Available capacity per resource, e.g. {'cpu': 2.0, 'mem_mb': 4096.0}.
    """

    capacity: dict[str, float]


@dataclass(frozen=True)
class TenantObs:
    """Runtime telemetry observation for a tenant.

    Attributes:
        usage: Actual resource usage, e.g. {'cpu': 0.85, 'mem_mb': 320.0}.
        throttle_ratio: Ratio of throttled time to quota period [0.0, 1.0].
        backlog: Number of pending queued requests in the tenant worker queue.
    """

    usage: dict[str, float]
    throttle_ratio: float = 0.0
    backlog: int = 0


@dataclass(frozen=True)
class Plan:
    """Result of an allocation decision across all active tenants.

    Attributes:
        alloc: Map of tenant_id -> {resource_key: allocated_amount}.
        unmet_guarantees: List of (tenant_id, resource_key) tuples where minimum was not met.
        notes: Informational or warning messages explaining solver choices or fallbacks.
        solve_ms: Time taken by the allocation solver in milliseconds.
    """

    alloc: dict[str, dict[str, float]]
    unmet_guarantees: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    solve_ms: float = 0.0
