"""Asynchronous open-loop load generator executing traces against worker containers."""

import asyncio
import time
from dataclasses import dataclass

import httpx

from alloc_platform.workload.traces import Arrival


@dataclass
class RequestSample:
    """Telemetry sample for an individual workload request.

    Provenance is strictly MEASURED.
    """

    run_id: str
    arrival_id: str
    tenant_id: str
    scheduled_time_s: float
    dispatch_time_s: float
    completion_time_s: float
    latency_ms: float
    service_ms: float
    status: str  # "ok", "deadline_missed", "rejected", "timeout", "error"
    is_censored: bool = False
    provenance: str = "MEASURED"


async def _dispatch_single_request(
    client: httpx.AsyncClient,
    arrival: Arrival,
    endpoint: str,
    run_id: str,
    t_zero: float,
    samples_list: list[RequestSample],
    semaphore: asyncio.Semaphore,
    hard_timeout_s: float = 10.0,
) -> None:
    async with semaphore:
        dispatch_rel_s = time.time() - t_zero
        url = f"{endpoint.rstrip('/')}/task"
        payload = {
            "cpu_units": arrival.cpu_units,
            "mem_mb": arrival.mem_mb,
            "hold_ms": arrival.hold_ms,
        }

        status = "error"
        service_ms = 0.0
        is_censored = False

        # Use generous hard timeout to allow true latency observation without clipping
        req_timeout = max(hard_timeout_s, arrival.deadline_ms / 1000.0 * 5.0)

        try:
            resp = await client.post(url, json=payload, timeout=req_timeout)
            completion_rel_s = time.time() - t_zero
            latency_ms = (completion_rel_s - arrival.scheduled_time_s) * 1000.0

            if resp.status_code == 200:
                data = resp.json()
                service_ms = float(data.get("service_ms", 0.0))
                if latency_ms > arrival.deadline_ms:
                    status = "deadline_missed"
                else:
                    status = "ok"
            elif resp.status_code == 429:
                status = "rejected"
            else:
                status = "error"

        except httpx.TimeoutException:
            completion_rel_s = time.time() - t_zero
            latency_ms = (completion_rel_s - arrival.scheduled_time_s) * 1000.0
            status = "timeout"
            is_censored = True
        except Exception:
            completion_rel_s = time.time() - t_zero
            latency_ms = (completion_rel_s - arrival.scheduled_time_s) * 1000.0
            status = "error"

        samples_list.append(
            RequestSample(
                run_id=run_id,
                arrival_id=arrival.arrival_id,
                tenant_id=arrival.tenant_id,
                scheduled_time_s=arrival.scheduled_time_s,
                dispatch_time_s=round(dispatch_rel_s, 4),
                completion_time_s=round(completion_rel_s, 4),
                latency_ms=round(latency_ms, 2),
                service_ms=round(service_ms, 2),
                status=status,
                is_censored=is_censored,
                provenance="MEASURED",
            )
        )



async def run_load(
    trace: list[Arrival],
    tenant_endpoints: dict[str, str],
    run_id: str,
    stop_event: asyncio.Event,
    max_inflight: int = 50,
) -> list[RequestSample]:
    """Dispatches requests open-loop according to their scheduled timestamps.

    Eliminates coordinated omission by computing latency relative to scheduled arrival time.
    """
    samples: list[RequestSample] = []
    semaphore = asyncio.Semaphore(max_inflight)
    tasks: list[asyncio.Task] = []

    t_zero = time.time()

    async with httpx.AsyncClient(
        limits=httpx.Limits(max_keepalive_connections=50, max_connections=100)
    ) as client:
        for arrival in trace:
            if stop_event.is_set():
                break

            now_rel_s = time.time() - t_zero
            wait_s = arrival.scheduled_time_s - now_rel_s

            if wait_s > 0:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=wait_s)
                    if stop_event.is_set():
                        break
                except TimeoutError:
                    pass

            endpoint = tenant_endpoints.get(arrival.tenant_id)
            if not endpoint:
                continue

            t = asyncio.create_task(
                _dispatch_single_request(
                    client=client,
                    arrival=arrival,
                    endpoint=endpoint,
                    run_id=run_id,
                    t_zero=t_zero,
                    samples_list=samples,
                    semaphore=semaphore,
                )
            )
            tasks.append(t)

        # Await completion of all dispatched tasks
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    return samples
