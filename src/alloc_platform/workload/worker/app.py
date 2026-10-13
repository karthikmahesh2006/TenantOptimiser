"""Tenant worker container service.

Fixed Python 3.11 HTTP service exposing task execution with real CPU hashing and memory allocation.
Reads configuration strictly from environment variables.
"""

import asyncio
import hashlib
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, status
from pydantic import BaseModel, Field

# Configuration strictly from environment variables
CONCURRENCY = int(os.getenv("CONCURRENCY", "4"))
QUEUE_MAX = int(os.getenv("QUEUE_MAX", "20"))
CPU_UNIT_HASH_ROUNDS = int(os.getenv("CPU_UNIT_HASH_ROUNDS", "150"))
PAGE_SIZE = 4096

# Stats tracking
stats_lock = asyncio.Lock()
tasks_completed = 0
tasks_rejected = 0
total_service_ms = 0.0

executor = ThreadPoolExecutor(max_workers=CONCURRENCY)
queue_semaphore: asyncio.Semaphore


@asynccontextmanager
async def lifespan(app: FastAPI):
    global queue_semaphore
    queue_semaphore = asyncio.Semaphore(QUEUE_MAX)
    yield
    executor.shutdown(wait=False)


app = FastAPI(title="Alloc Tenant Worker", lifespan=lifespan)


class TaskRequest(BaseModel):
    cpu_units: float = Field(default=0.0, ge=0.0, le=100.0)
    mem_mb: float = Field(default=0.0, ge=0.0, le=4096.0)
    hold_ms: float = Field(default=0.0, ge=0.0, le=10000.0)


def _execute_sync_task(cpu_units: float, mem_mb: float, hold_ms: float) -> float:
    t_start = time.perf_counter()

    # 1. CPU work: SHA-256 over 64 KB buffer repeated N times (releases GIL)
    if cpu_units > 0:
        buf = b"A" * (64 * 1024)
        rounds = max(1, int(cpu_units * CPU_UNIT_HASH_ROUNDS))
        h = hashlib.sha256()
        for _ in range(rounds):
            h.update(buf)

    # 2. Memory work: Allocate bytearray, touch every page, hold, then release
    if mem_mb > 0 or hold_ms > 0:
        if mem_mb > 0:
            size_bytes = int(mem_mb * 1024 * 1024)
            data = bytearray(size_bytes)
            # Touch each 4 KB page
            for offset in range(0, size_bytes, PAGE_SIZE):
                data[offset] = offset % 251
        if hold_ms > 0:
            time.sleep(hold_ms / 1000.0)

    return (time.perf_counter() - t_start) * 1000.0


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "healthy"}


@app.get("/stats")
async def get_stats() -> dict[str, Any]:
    async with stats_lock:
        avg_ms = (total_service_ms / tasks_completed) if tasks_completed > 0 else 0.0
        return {
            "completed": tasks_completed,
            "rejected": tasks_rejected,
            "total_service_ms": round(total_service_ms, 2),
            "avg_service_ms": round(avg_ms, 2),
            "concurrency": CONCURRENCY,
            "queue_max": QUEUE_MAX,
        }


@app.post("/task")
async def execute_task(req: TaskRequest) -> dict[str, Any]:
    global tasks_completed, tasks_rejected, total_service_ms

    # Bounded queue check: acquire without blocking if full
    if queue_semaphore.locked():
        async with stats_lock:
            tasks_rejected += 1
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Worker queue capacity exceeded",
        )

    await queue_semaphore.acquire()
    try:
        loop = asyncio.get_running_loop()
        service_ms = await loop.run_in_executor(
            executor, _execute_sync_task, req.cpu_units, req.mem_mb, req.hold_ms
        )
        async with stats_lock:
            tasks_completed += 1
            total_service_ms += service_ms
        return {
            "status": "ok",
            "service_ms": round(service_ms, 2),
        }
    finally:
        queue_semaphore.release()
