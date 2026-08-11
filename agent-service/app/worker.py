"""Redis Streams worker for durable Agent run dispatch."""

import asyncio
import os

from redis.asyncio import Redis

from . import main


STREAM = "agent-runs"
GROUP = "agent-workers"


async def process_entry(redis: Redis, consumer: str, entry_id: str, run_id: str, recover: bool, resume_confirmation: bool = False) -> None:
    task = asyncio.create_task(main.orchestrate_run(run_id, recover=recover, resume_confirmation=resume_confirmation))
    try:
        while not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=10)
            except asyncio.TimeoutError:
                # Refresh pending idle time so XAUTOCLAIM only sees work whose
                # process has actually stopped heartbeating.
                await redis.xclaim(STREAM, GROUP, consumer, min_idle_time=0, message_ids=[entry_id], justid=True)
        await task
        await redis.xack(STREAM, GROUP, entry_id)
    except Exception:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise


async def run() -> None:
    main.settings.validate()
    main.store = main.AgentStore(main.settings.database_url or main.settings.database_path, main.settings.master_key)
    redis = Redis.from_url(os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0"), decode_responses=True)
    try:
        try:
            await redis.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        consumer = "worker-{}".format(os.getpid())
        while True:
            # Reclaim deliveries left pending by a crashed worker before taking new work.
            claimed = await redis.xautoclaim(STREAM, GROUP, consumer, min_idle_time=30_000, start_id="0-0", count=10)
            for entry_id, fields in claimed[1]:
                run_id = fields.get("run_id")
                if run_id:
                    await process_entry(redis, consumer, entry_id, run_id, recover=True, resume_confirmation=fields.get("resume_confirmation") == "True")
                else:
                    await redis.xack(STREAM, GROUP, entry_id)
            batches = await redis.xreadgroup(GROUP, consumer, {STREAM: ">"}, count=1, block=5000)
            for _, entries in batches:
                for entry_id, fields in entries:
                    run_id = fields.get("run_id")
                    if run_id:
                        await process_entry(redis, consumer, entry_id, run_id, recover=False, resume_confirmation=fields.get("resume_confirmation") == "True")
                    else:
                        await redis.xack(STREAM, GROUP, entry_id)
    finally:
        await redis.aclose()
        if main.store is not None:
            main.store.db.close()


if __name__ == "__main__":
    asyncio.run(run())
