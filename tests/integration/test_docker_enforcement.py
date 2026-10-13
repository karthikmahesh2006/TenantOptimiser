"""Integration tests for Docker container enforcement and cgroup v2 verification."""

import time

import pytest

from alloc_platform.enforcement.cgroup import CgroupReader
from alloc_platform.enforcement.docker_enforcer import DockerEnforcer, TenantContainerSpec


@pytest.mark.needs_docker
def test_docker_enforcer_lifecycle_and_cgroup_limits():
    """Verify container creation, dynamic cgroup limit adjustment, and cleanup on host."""
    try:
        enforcer = DockerEnforcer()
        enforcer.client.ping()
    except Exception as e:
        pytest.skip(f"Docker proxy unreachable: {e}")

    run_id = f"test_run_{int(time.time())}"
    tenant_id = "test_tenant_alpha"

    enforcer.ensure_network()

    spec = TenantContainerSpec(
        tenant_id=tenant_id,
        run_id=run_id,
        cpu_cores=0.5,
        mem_mb=128.0,
    )

    container_id = enforcer.create_tenant(spec)
    assert container_id is not None

    try:
        c = enforcer.client.containers.get(container_id)
        assert c.status in ("running", "created")

        # Dynamically apply new limits
        res = enforcer.apply_limits(container_id, cpu_cores=1.0, mem_mb=256.0)
        assert res.success is True
        assert res.apply_ms >= 0.0

        # Verify limits via cgroup reader
        reader = CgroupReader(container_id)
        cpu_cores, mem_mb = reader.read_limits()
        if cpu_cores is not None:
            assert abs(cpu_cores - 1.0) < 0.05
        if mem_mb is not None:
            assert abs(mem_mb - 256.0) < 5.0
    finally:
        cleaned = enforcer.stop_and_remove_all(run_id)
        assert cleaned >= 1
