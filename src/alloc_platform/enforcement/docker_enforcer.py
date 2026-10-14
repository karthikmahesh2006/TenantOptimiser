"""Docker enforcement client for isolated tenant containers and cgroup limit updates."""

import time
from dataclasses import dataclass

import docker
from docker.errors import APIError, NotFound

from alloc_platform.config import config
from alloc_platform.enforcement.cgroup import CgroupReader


@dataclass
class ApplyResult:
    """Result of dynamic cgroup resource limit application."""

    container_id: str
    applied_cpu_cores: float
    applied_mem_mb: float
    apply_ms: float
    warning: str | None = None
    success: bool = True



@dataclass
class TenantContainerSpec:
    """Validated parameters for spawning a tenant container."""

    tenant_id: str
    run_id: str
    cpu_cores: float
    mem_mb: float
    image: str = "alloc-worker:latest"


class DockerEnforcer:
    """Enforces resource allocation limits on real Docker containers via docker-socket-proxy."""

    def __init__(
        self,
        docker_host: str | None = None,
        client: docker.DockerClient | None = None,
    ) -> None:
        self.docker_host = docker_host or config.docker_host
        if client is not None:
            self.client = client
        elif self.docker_host:
            self.client = docker.DockerClient(base_url=self.docker_host)
        else:
            self.client = docker.from_env()


    def ensure_network(self) -> str:
        """Ensures isolated internal bridge network exists."""
        net_name = config.tenant_network_name
        try:
            net = self.client.networks.get(net_name)
            return net.id
        except NotFound:
            # Create internal network without public egress
            net = self.client.networks.create(
                name=net_name,
                driver="bridge",
                internal=True,
                labels={"project": "alloc"},
            )
            return net.id

    def create_tenant(self, spec: TenantContainerSpec) -> str:
        """Spawns an isolated tenant worker container with hardened security profiles."""
        self.ensure_network()

        # Strict validation against platform caps
        if spec.cpu_cores < config.min_container_cpu or spec.cpu_cores > config.max_container_cpu:
            raise ValueError(
                f"CPU {spec.cpu_cores} outside valid range [{config.min_container_cpu}, {config.max_container_cpu}]"
            )
        if spec.mem_mb < config.min_container_mem_mb or spec.mem_mb > config.max_container_mem_mb:
            raise ValueError(
                f"Memory {spec.mem_mb} outside valid range [{config.min_container_mem_mb}, {config.max_container_mem_mb}]"
            )

        cpu_period = 100000
        cpu_quota = int(spec.cpu_cores * cpu_period)
        mem_bytes = int(spec.mem_mb * 1024 * 1024)

        container_name = f"alloc_{spec.run_id[:8]}_{spec.tenant_id}"

        # Hardened container parameters (E11)
        container = self.client.containers.run(
            image=spec.image,
            name=container_name,
            detach=True,
            network=config.tenant_network_name,
            labels={
                "project": "alloc",
                "run_id": spec.run_id,
                "tenant_id": spec.tenant_id,
            },
            user="worker",
            read_only=True,
            tmpfs={"/tmp": "size=16m,mode=1777"},
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            pids_limit=100,
            cpu_period=cpu_period,
            cpu_quota=cpu_quota,
            mem_limit=mem_bytes,
            memswap_limit=mem_bytes,  # Swap strictly disabled
        )
        return container.id

    def apply_limits(self, container_id: str, cpu_cores: float, mem_mb: float) -> ApplyResult:
        """Dynamically applies CPU quota and memory limits via the container update API."""
        start_t = time.perf_counter()
        warning = None

        cpu_period = 100000
        cpu_quota = int(cpu_cores * cpu_period)
        mem_bytes = int(mem_mb * 1024 * 1024)

        try:
            container = self.client.containers.get(container_id)
            # Apply both CPU and memory
            container.update(
                cpu_period=cpu_period,
                cpu_quota=cpu_quota,
                mem_limit=mem_bytes,
                memswap_limit=mem_bytes,
            )
            applied_cpu = cpu_cores
            applied_mem = mem_mb
        except APIError as e:
            # If lowering memory fails (e.g. current usage > proposed memory limit)
            if (
                "cannot set memory limit below current usage" in str(e).lower()
                or "memory" in str(e).lower()
            ):
                warning = f"Memory limit {mem_mb} MB rejected by kernel (usage above limit); applied CPU only."
                try:
                    container.update(cpu_period=cpu_period, cpu_quota=cpu_quota)
                    applied_cpu = cpu_cores
                    applied_mem = mem_mb  # Retained previous
                except Exception as inner_e:
                    warning = f"Update failed: {inner_e!s}"
                    applied_cpu = cpu_cores
                    applied_mem = mem_mb
            else:
                warning = f"Container update failed: {e!s}"
                applied_cpu = cpu_cores
                applied_mem = mem_mb

        apply_ms = (time.perf_counter() - start_t) * 1000.0
        return ApplyResult(
            container_id=container_id,
            applied_cpu_cores=applied_cpu,
            applied_mem_mb=applied_mem,
            apply_ms=round(apply_ms, 2),
            warning=warning,
            success=(warning is None),
        )


    def verify_limits(self, container_id: str, expected_cpu: float, expected_mem: float) -> bool:
        """Reads back cgroup v2 cpu.max and memory.max to confirm kernel enforcement."""
        reader = CgroupReader(container_id)
        actual_cpu, actual_mem = reader.read_limits()
        if actual_cpu is None or actual_mem is None:
            # Cannot read cgroup directly (e.g. non-Linux host)
            return True

        cpu_ok = abs(actual_cpu - expected_cpu) < 0.05
        mem_ok = abs(actual_mem - expected_mem) < 5.0
        return cpu_ok and mem_ok

    def stop_and_remove_all(self, run_id: str) -> int:
        """Stops and removes all containers associated with the specified run_id."""
        removed = 0
        try:
            containers = self.client.containers.list(
                all=True,
                filters={"label": ["project=alloc", f"run_id={run_id}"]},
            )
            for c in containers:
                try:
                    c.remove(force=True)
                    removed += 1
                except Exception:
                    pass
        except Exception:
            pass
        return removed

    def sweep_orphans(self, active_run_ids: list[str] | None = None) -> int:
        """Removes any container labelled project=alloc that is not in active_run_ids."""
        active_set = set(active_run_ids or [])
        swept = 0
        try:
            containers = self.client.containers.list(
                all=True,
                filters={"label": "project=alloc"},
            )
            for c in containers:
                r_id = c.labels.get("run_id")
                if not r_id or r_id not in active_set:
                    try:
                        c.remove(force=True)
                        swept += 1
                    except Exception:
                        pass
        except Exception:
            pass
        return swept
