from __future__ import annotations

import time

from arq import cron
from arq.connections import RedisSettings

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.infrastructure.database import Database
from app.infrastructure.redis import RedisManager
from app.services.auto import AutoRunService
from app.services.registration import RegistrationWorker
from app.services.sms_cleanup import SMSCleanupService
from app.services.team_rotation import TeamRotationService
from app.services.team_sso import TeamSSOService

settings = get_settings()
configure_logging(settings.log_level)


async def run_registration(ctx: dict, run_id: str) -> None:
    worker = RegistrationWorker(
        ctx["database"].session_factory,
        ctx["redis"],
        settings,
    )
    await worker.execute(run_id)


async def run_auto_tick(ctx: dict) -> None:
    await AutoRunService(ctx["database"].session_factory, ctx["redis"], settings).tick()


async def run_team_sso_sync(ctx: dict) -> None:
    service = TeamSSOService(ctx["database"].session_factory, settings)
    await service.dispatch_once()


async def run_sms_cleanup(ctx: dict) -> None:
    service = SMSCleanupService(ctx["database"].session_factory)
    await service.dispatch_once()


async def run_team_rotation_dispatch(ctx: dict, force: bool = False, mother_id: str = "") -> None:
    """Find due mothers and enqueue one resumable job per mother."""
    service = TeamRotationService(
        ctx["database"].session_factory,
        settings,
        redis=ctx["redis"],
    )
    async with ctx["database"].session() as session:
        config = await service._config(session)
    if mother_id:
        mother_ids = await service.claim_due(force=True, limit=1, mother_id=str(mother_id))
    else:
        mother_ids = await service.claim_due(force=force, limit=max(10, config["mother_concurrency"] * 8))
    for mother_id in mother_ids:
        try:
            await ctx["redis"].enqueue(
                "run_team_rotation_mother",
                job_id=f"team-rotation-{mother_id}-{int(time.time() * 1000)}",
                mother_id=mother_id,
            )
        except Exception:
            # Put the cursor back immediately; a transient Redis failure must
            # not hide one complete interval of work in PostgreSQL.
            async with ctx["database"].session() as session:
                async with session.begin():
                    await service.team.schedule_mother(session, mother_id, when=time.time())
            raise


async def run_team_rotation_mother(ctx: dict, mother_id: str) -> None:
    service = TeamRotationService(
        ctx["database"].session_factory,
        settings,
        redis=ctx["redis"],
    )
    await service.process_mother(str(mother_id))


async def startup(ctx: dict) -> None:
    database = Database(settings)
    redis = RedisManager(settings)
    ctx["database"] = database
    ctx["redis"] = redis
    if settings.auto_create_schema:
        await database.create_schema()


async def shutdown(ctx: dict) -> None:
    await ctx["redis"].close()
    await ctx["database"].close()


class WorkerSettings:
    functions = [
        run_registration,
        run_auto_tick,
        run_team_sso_sync,
        run_sms_cleanup,
        run_team_rotation_dispatch,
        run_team_rotation_mother,
    ]
    cron_jobs = [
        cron(
            run_team_sso_sync,
            name="team-sso-dispatch",
            second={0, 15, 30, 45},
            timeout=settings.worker_job_timeout,
        ),
        cron(
            run_sms_cleanup,
            name="sms-cleanup",
            second={5, 20, 35, 50},
            timeout=settings.worker_job_timeout,
        ),
        cron(
            run_team_rotation_dispatch,
            name="team-rotation-dispatch",
            second=set(range(0, 60, 5)),
            timeout=settings.worker_job_timeout,
        ),
        cron(
            run_auto_tick,
            name="auto-registration-tick",
            second={0, 10, 20, 30, 40, 50},
            timeout=settings.worker_job_timeout,
        ),
    ]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    queue_name = settings.redis_queue_name
    max_jobs = settings.worker_max_jobs
    job_timeout = settings.worker_job_timeout
    on_startup = startup
    on_shutdown = shutdown


def run() -> None:
    from arq import Worker

    worker = Worker(
        functions=WorkerSettings.functions,
        cron_jobs=WorkerSettings.cron_jobs,
        redis_settings=WorkerSettings.redis_settings,
        queue_name=WorkerSettings.queue_name,
        max_jobs=WorkerSettings.max_jobs,
        job_timeout=WorkerSettings.job_timeout,
        on_startup=WorkerSettings.on_startup,
        on_shutdown=WorkerSettings.on_shutdown,
    )
    worker.run()


if __name__ == "__main__":
    run()
