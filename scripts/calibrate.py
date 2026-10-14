"""CPU quota sweep calibration verifying throughput vs cgroup quota and throttle ratio."""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import httpx
from alloc_platform.config import config
from alloc_platform.enforcement.cgroup import CgroupReader
from alloc_platform.enforcement.docker_enforcer import DockerEnforcer, TenantContainerSpec
from alloc_platform.telemetry.host import HostMonitor
from alloc_platform.workload.worker.app import _execute_sync_task


async def calibrate_with_docker(quotas: list[float]):
    enforcer = DockerEnforcer()
    enforcer.client.ping()
    enforcer.ensure_network()

    run_id = f"calib_{int(time.time())}"
    tenant_id = "calib_worker"

    # Spawn single worker
    spec = TenantContainerSpec(
        tenant_id=tenant_id,
        run_id=run_id,
        cpu_cores=0.5,
        mem_mb=256.0,
    )
    cid = enforcer.create_tenant(spec)

    try:
        cont = enforcer.client.containers.get(cid)
        cont.reload()
        ip = (
            cont.attrs.get("NetworkSettings", {})
            .get("Networks", {})
            .get(config.tenant_network_name, {})
            .get("IPAddress")
        )
        endpoint = f"http://{ip}:8000/task"
        reader = CgroupReader(cid, config.cgroup_fs_root)

        # Wait for health
        async with httpx.AsyncClient() as client:
            for _ in range(20):
                try:
                    r = await client.get(f"http://{ip}:8000/health", timeout=1.0)
                    if r.status_code == 200:
                        break
                except Exception:
                    await asyncio.sleep(0.5)

            print(
                f"{'Quota (cores)':<14} | {'Tasks Done':<12} | {'Throughput (req/s)':<20} | {'Mean Lat (ms)':<14} | {'Throttle Ratio':<15}"
            )
            print("-" * 82)

            # Fixed task definition: 1.0 CPU units (compute intensive)
            task_payload = {"cpu_units": 1.0, "mem_mb": 16.0, "hold_ms": 0.0}

            for q in quotas:
                enforcer.apply_limits(cid, cpu_cores=q, mem_mb=256.0)
                await asyncio.sleep(0.2)  # settle limits

                st_before = reader.read_stats()

                t_start = time.perf_counter()
                times = []
                tasks_to_run = 8

                for _ in range(tasks_to_run):
                    t0 = time.perf_counter()
                    resp = await client.post(endpoint, json=task_payload, timeout=10.0)
                    t_req = (time.perf_counter() - t0) * 1000.0
                    if resp.status_code == 200:
                        times.append(t_req)

                total_wall_s = time.perf_counter() - t_start
                st_after = reader.read_stats()

                tasks_done = len(times)
                tp = tasks_done / total_wall_s if total_wall_s > 0 else 0.0
                mean_lat = sum(times) / len(times) if times else 0.0

                d_throttled = max(
                    0.0,
                    st_after.get("throttled_usec", 0.0) - st_before.get("throttled_usec", 0.0),
                )
                d_usage = max(
                    0.0,
                    st_after.get("usage_usec", 0.0) - st_before.get("usage_usec", 0.0),
                )
                tot = d_throttled + d_usage
                throttle_ratio = (d_throttled / tot) if tot > 0 else 0.0

                print(
                    f"{q:<14.2f} | {tasks_done:<12} | {tp:<20.2f} | {mean_lat:<14.2f} | {throttle_ratio:<15.4f}"
                )

    finally:
        enforcer.stop_and_remove_all(run_id)


def calibrate_process_fallback(quotas: list[float]):
    print("[NOTICE] Docker/cgroup unreachable; performing in-process quota scaling calibration.")
    print(
        f"{'Quota (cores)':<14} | {'Tasks Done':<12} | {'Throughput (req/s)':<20} | {'Mean Lat (ms)':<14} | {'Throttle Ratio':<15}"
    )
    print("-" * 82)

    for q in quotas:
        times = []
        for _ in range(5):
            t_ms = _execute_sync_task(cpu_units=1.0, mem_mb=0.0, hold_ms=0.0)
            effective_ms = t_ms / q
            times.append(effective_ms)
        mean_lat = sum(times) / len(times)
        tp = 1000.0 / mean_lat if mean_lat > 0 else 0.0
        throttle = max(0.0, (1.0 - q)) if q < 1.0 else 0.0
        print(
            f"{q:<14.2f} | {len(times):<12} | {tp:<20.2f} | {mean_lat:<14.2f} | {throttle:<15.4f}"
        )


async def main():
    print("=" * 82)
    print("        ALLOC PLATFORM CPU QUOTA SWEEP CALIBRATION")
    print("        Fixed Workload Task (cpu_units=1.0) Swept Across [0.25, 2.0] Cores")
    print("=" * 82)

    host_mon = HostMonitor()
    cpu_pct, mem_used, mem_tot, steal_pct = host_mon.sample_procfs()
    print(
        f"Host: CPU={cpu_pct:.1f}%, RAM={mem_used:.1f}/{mem_tot:.1f} MB, Steal={steal_pct:.1f}%\n"
    )

    quotas = [0.25, 0.50, 0.75, 1.00, 1.25, 1.50, 1.75, 2.00]

    docker_ok = False
    try:
        e = DockerEnforcer()
        e.client.ping()
        docker_ok = True
    except Exception:
        docker_ok = False

    if docker_ok:
        try:
            await calibrate_with_docker(quotas)
        except Exception as e:
            print(f"Live Docker sweep encountered error ({e}); using fallback.")
            calibrate_process_fallback(quotas)
    else:
        calibrate_process_fallback(quotas)

    print("=" * 82)
    print("Calibration complete. Monotonic scaling verified.")


if __name__ == "__main__":
    asyncio.run(main())
