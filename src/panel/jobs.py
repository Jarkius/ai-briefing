"""In-process background job registry.

Two hard rules from the panel-readiness review (M1/m5):

1. Blocking core functions (generate(), send_two_part_briefing()) run via
   asyncio.to_thread — awaiting them inline would freeze uvicorn's single
   event loop for minutes (urllib retries, time.sleep backoff, subprocess).
2. Jobs die with the server (documented on-demand-lifecycle tradeoff); no
   persistence. A job that raises records status="error", never hangs the
   UI on "running".
"""

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Job:
    name: str
    status: str = "running"  # running | done | error
    phase_text: str = "starting…"
    result: object = None
    error: str = ""
    started_at: str = field(default_factory=lambda: datetime.now().strftime("%H:%M:%S"))


JOBS: dict[str, Job] = {}


def any_running() -> bool:
    return any(j.status == "running" for j in JOBS.values())


def get(job_id: str) -> Job | None:
    return JOBS.get(job_id)


def submit_sync(name: str, fn, *args, **kwargs) -> str:
    """Run a BLOCKING function on a worker thread, tracked as a job.
    Returns the job id immediately."""
    job_id = uuid.uuid4().hex[:12]
    job = Job(name=name)
    JOBS[job_id] = job

    async def _runner():
        try:
            job.result = await asyncio.to_thread(fn, *args, **kwargs)
            job.status = "done"
            job.phase_text = "done"
        except Exception as e:
            job.status = "error"
            job.error = str(e)
            job.phase_text = f"error: {e}"

    asyncio.get_running_loop().create_task(_runner())
    return job_id


def submit_async(name: str, coro_fn, *args, **kwargs) -> str:
    """Run a genuinely-async callable (e.g. MCP research) as a job.
    The callable receives a phase(text) callback as its first argument."""
    job_id = uuid.uuid4().hex[:12]
    job = Job(name=name)
    JOBS[job_id] = job

    def phase(text: str):
        job.phase_text = text

    async def _runner():
        try:
            job.result = await coro_fn(phase, *args, **kwargs)
            job.status = "done"
            job.phase_text = "done"
        except Exception as e:
            job.status = "error"
            job.error = str(e)
            job.phase_text = f"error: {e}"

    asyncio.get_running_loop().create_task(_runner())
    return job_id
