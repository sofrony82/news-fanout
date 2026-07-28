import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from typing import AsyncIterator

from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

from news_fanout.adapters import LoggingPushSender, RateLimiter, StubArticleSource, StubTopicClassifier
from news_fanout.api import health_router, internal_router, v1_router
from news_fanout.config import AppSettings, Role, get_app_settings
from news_fanout.db import connect
from news_fanout.dedup import PushDedupStore, create_redis_client
from news_fanout.migrations import apply_schema
from news_fanout.workers import ClassifierWorker, DigestDispatcher, IngestJob, PushCoordinator, PushWorker

logger = logging.getLogger(__name__)


def _background_coroutines(app: FastAPI) -> list[asyncio.Task[None]]:
    settings: AppSettings = app.state.settings
    session_maker = app.state.session_maker
    role = settings.role
    tasks: list[asyncio.Task[None]] = []

    if role in (Role.INGEST, Role.ALL):
        source = StubArticleSource(settings.ingest.articles_per_page, settings.ingest.stub_max_page_id)
        ingest = IngestJob(settings.ingest, session_maker, source)
        tasks.append(asyncio.create_task(ingest.run_forever()))

    if role in (Role.CLASSIFIER, Role.ALL):
        classifier = ClassifierWorker(
            settings.classifier,
            session_maker,
            StubTopicClassifier(settings.classifier.max_topics_per_article),
        )
        tasks.append(asyncio.create_task(classifier.run_forever()))

    if role in (Role.PUSH, Role.ALL):
        dispatcher = DigestDispatcher(
            settings.push,
            session_maker,
            LoggingPushSender(),
            RateLimiter(settings.push.sends_per_second),
            app.state.dedup_store,
        )
        tasks.append(asyncio.create_task(PushCoordinator(settings.push, session_maker, dispatcher).run_forever()))
        for index in range(settings.push.worker_count):
            worker = PushWorker(settings.push, session_maker, dispatcher, index)
            tasks.append(asyncio.create_task(worker.run_forever()))

    return tasks


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: AppSettings = app.state.settings
    if settings.server.auto_migrate:
        await apply_schema(app.state.engine)
    tasks = _background_coroutines(app)
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        await app.state.dedup_store.close()
        await app.state.engine.dispose()


def create_app(settings: AppSettings | None = None) -> FastAPI:
    app_settings = settings or get_app_settings()
    app = FastAPI(title="news-fanout", lifespan=lifespan)
    engine, session_maker = connect(app_settings.database)
    app.state.settings = app_settings
    app.state.engine = engine
    app.state.session_maker = session_maker
    app.state.dedup_store = PushDedupStore(
        create_redis_client(app_settings.redis),
        app_settings.redis.key_prefix,
        app_settings.push.dedup_ttl_seconds,
    )

    Instrumentator(should_instrument_requests_inprogress=True).instrument(app).expose(app, include_in_schema=False)
    app.include_router(health_router)
    if app_settings.server.expose_internal_stats:
        app.include_router(internal_router)
    if app_settings.role in (Role.API, Role.ALL):
        app.include_router(v1_router)
    return app
