"""S2 tests: the job registry's dispatch, error capture, and — the one that
matters most (review M1) — proof that a blocking job does NOT block the
event loop, because submit_sync routes it through asyncio.to_thread.
"""

import asyncio
import time

from panel import jobs


def _run(coro):
    return asyncio.run(coro)


def test_submit_sync_completes_and_stores_result():
    async def main():
        job_id = jobs.submit_sync("t", lambda: 41 + 1)
        for _ in range(100):
            await asyncio.sleep(0.01)
            if jobs.JOBS[job_id].status != "running":
                break
        return job_id

    job_id = _run(main())
    job = jobs.JOBS.pop(job_id)
    assert job.status == "done"
    assert job.result == 42


def test_submit_sync_captures_exception_as_error_status():
    def boom():
        raise RuntimeError("kaboom")

    async def main():
        job_id = jobs.submit_sync("t", boom)
        for _ in range(100):
            await asyncio.sleep(0.01)
            if jobs.JOBS[job_id].status != "running":
                break
        return job_id

    job_id = _run(main())
    job = jobs.JOBS.pop(job_id)
    assert job.status == "error"
    assert "kaboom" in job.error
    assert "kaboom" in job.phase_text  # pollers see the failure, never hang


def test_blocking_job_does_not_block_event_loop():
    # The M1 regression guard: while a 0.5s time.sleep job runs, the loop
    # must keep ticking. If submit_sync inline-awaited the callable, the
    # probe counter would be starved and stay near zero.
    def blocking():
        time.sleep(0.5)
        return "slept"

    async def main():
        ticks = 0
        job_id = jobs.submit_sync("t", blocking)

        async def probe():
            nonlocal ticks
            while jobs.JOBS[job_id].status == "running":
                ticks += 1
                await asyncio.sleep(0.01)

        await asyncio.wait_for(probe(), timeout=5)
        return job_id, ticks

    job_id, ticks = _run(main())
    job = jobs.JOBS.pop(job_id)
    assert job.status == "done"
    assert ticks > 20, f"event loop starved: only {ticks} ticks during a 0.5s blocking job"


def test_submit_async_reports_phases_via_callback():
    async def researchy(phase):
        phase("step one")
        await asyncio.sleep(0.01)
        phase("step two")
        return "findings"

    async def main():
        job_id = jobs.submit_async("t", researchy)
        seen = set()
        for _ in range(200):
            seen.add(jobs.JOBS[job_id].phase_text)
            if jobs.JOBS[job_id].status != "running":
                break
            await asyncio.sleep(0.005)
        return job_id, seen

    job_id, seen = _run(main())
    job = jobs.JOBS.pop(job_id)
    assert job.status == "done"
    assert job.result == "findings"
    assert "step two" in seen or job.phase_text == "done"


def test_submit_async_error_is_captured():
    async def bad(phase):
        phase("about to fail")
        raise ValueError("async kaboom")

    async def main():
        job_id = jobs.submit_async("t", bad)
        for _ in range(100):
            await asyncio.sleep(0.01)
            if jobs.JOBS[job_id].status != "running":
                break
        return job_id

    job_id = _run(main())
    job = jobs.JOBS.pop(job_id)
    assert job.status == "error"
    assert "async kaboom" in job.error


def test_any_running_reflects_registry_state():
    assert jobs.any_running() is False
    jobs.JOBS["x"] = jobs.Job(name="probe")
    try:
        assert jobs.any_running() is True
        jobs.JOBS["x"].status = "done"
        assert jobs.any_running() is False
    finally:
        del jobs.JOBS["x"]
