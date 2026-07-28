import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from news_fanout import repository
from news_fanout.adapters import ArticleSource, PushSender, RateLimiter, TopicClassifier
from news_fanout.config import ClassifierSettings, IngestSettings, PushSettings
from news_fanout.dedup import PushDedupStore
from news_fanout.models import PushMessage, PushPageSpec, TopicDigest

logger = logging.getLogger(__name__)


async def run_loop(name: str, idle_interval_seconds: float, step: Callable[[], Awaitable[int]]) -> None:
    logger.info("starting loop %s", name)
    while True:
        try:
            processed = await step()
        except Exception:
            logger.exception("loop %s failed", name)
            processed = 0
        if processed == 0:
            await asyncio.sleep(idle_interval_seconds)


class IngestJob:
    def __init__(
        self,
        settings: IngestSettings,
        session_maker: async_sessionmaker[AsyncSession],
        source: ArticleSource,
    ) -> None:
        self._settings = settings
        self._session_maker = session_maker
        self._source = source

    async def run_forever(self) -> None:
        await run_loop("ingest", self._settings.idle_interval_seconds, self.run_once)

    async def run_once(self) -> int:
        async with self._session_maker() as session:
            due_sources = await repository.claim_due_sources(session, self._settings.sources_per_cycle)
            await session.commit()

        ingested = 0
        for due_source in due_sources:
            ingested += await self._poll_source(due_source.source_id, due_source.page_id_from)
        return ingested

    async def _poll_source(self, source_id: str, page_id_from: int) -> int:
        page = await self._source.get_articles(source_id, page_id_from, self._settings.articles_per_page)
        if not page.articles:
            return 0

        with_bodies = [(article, await self._source.fetch_body(article)) for article in page.articles]
        async with self._session_maker() as session:
            article_ids = await repository.store_articles(session, source_id, with_bodies)
            await repository.advance_source_cursor(session, source_id, page.page_id_to)
            await session.commit()

        logger.info("ingested source=%s new_articles=%s page_id_to=%s", source_id, len(article_ids), page.page_id_to)
        return len(article_ids)


class ClassifierWorker:
    def __init__(
        self,
        settings: ClassifierSettings,
        session_maker: async_sessionmaker[AsyncSession],
        classifier: TopicClassifier,
    ) -> None:
        self._settings = settings
        self._session_maker = session_maker
        self._classifier = classifier
        self._worker_id = f"classifier-{uuid.uuid4().hex[:8]}"

    async def run_forever(self) -> None:
        await run_loop("classifier", self._settings.idle_interval_seconds, self.run_once)

    async def run_once(self) -> int:
        async with self._session_maker() as session:
            jobs = await repository.claim_classify_jobs(
                session,
                self._worker_id,
                self._settings.batch_size,
                self._settings.lease_seconds,
            )
            await session.commit()
            if not jobs:
                return 0
            topic_ids = await repository.list_topic_ids(session)

        for job in jobs:
            async with self._session_maker() as session:
                try:
                    await self._classify_one(session, job.article_id, topic_ids)
                    await repository.complete_classify_job(session, job.job_id)
                    await session.commit()
                except Exception as exc:
                    await session.rollback()
                    logger.exception("classification failed article_id=%s", job.article_id)
                    await repository.fail_classify_job(
                        session,
                        job,
                        repr(exc),
                        self._settings.max_attempts,
                        self._settings.retry_backoff_seconds,
                    )
                    await session.commit()
        return len(jobs)

    async def _classify_one(self, session: AsyncSession, article_id: int, topic_ids: list[str]) -> None:
        article = await repository.load_article_for_classification(session, article_id)
        if article is None:
            return
        assignments = await self._classifier.classify(article.title, article.body, topic_ids)
        accepted = sorted(
            (item for item in assignments if item.confidence >= self._settings.min_confidence),
            key=lambda item: item.confidence,
            reverse=True,
        )[: self._settings.max_topics_per_article]
        if not accepted:
            logger.info("article below confidence threshold article_id=%s", article_id)
            return
        await repository.publish_article_topics(session, article, accepted)


class DigestDispatcher:
    def __init__(
        self,
        settings: PushSettings,
        session_maker: async_sessionmaker[AsyncSession],
        sender: PushSender,
        rate_limiter: RateLimiter,
        dedup_store: PushDedupStore,
    ) -> None:
        self._settings = settings
        self._session_maker = session_maker
        self._sender = sender
        self._rate_limiter = rate_limiter
        self._dedup_store = dedup_store

    async def deliver_page(
        self,
        spec: PushPageSpec,
        on_checkpoint: Callable[[str], Awaitable[None]] | None = None,
    ) -> int:
        active_since = datetime.now(timezone.utc) - timedelta(days=self._settings.active_user_window_days)
        dedup_key = f"{spec.topic_id}:{spec.post_id_to}"
        message = PushMessage(
            topic_id=spec.topic_id,
            topic_name=spec.topic_name,
            article_count=spec.article_count,
            post_id_to=spec.post_id_to,
            title=spec.topic_name,
            body=f"{spec.article_count} new stories in {spec.topic_name}",
        )

        cursor = spec.cursor_checkpoint or spec.user_id_from
        sent = 0
        while True:
            async with self._session_maker() as session:
                targets = await repository.load_push_targets(
                    session,
                    spec.topic_id,
                    cursor,
                    spec.user_id_to,
                    active_since,
                    self._settings.multicast_batch_size,
                )
            if not targets:
                return sent

            cursor = targets[-1].user_id
            fresh_user_ids = await self._dedup_store.claim([target.user_id for target in targets], dedup_key)

            tokens = [target.device_token for target in targets if target.user_id in fresh_user_ids]
            if tokens:
                await self._rate_limiter.acquire(len(tokens))
                result = await self._sender.send_multicast(tokens, message)
                sent += result.sent
                if result.invalid_tokens:
                    async with self._session_maker() as session:
                        await repository.prune_device_tokens(session, result.invalid_tokens)
                        await session.commit()

            if on_checkpoint is not None:
                await on_checkpoint(cursor)
            if len(targets) < self._settings.multicast_batch_size:
                return sent


class PushCoordinator:
    def __init__(
        self,
        settings: PushSettings,
        session_maker: async_sessionmaker[AsyncSession],
        dispatcher: DigestDispatcher,
    ) -> None:
        self._settings = settings
        self._session_maker = session_maker
        self._dispatcher = dispatcher

    async def run_forever(self) -> None:
        await run_loop("push-coordinator", self._settings.coordinator_idle_interval_seconds, self.run_once)

    async def run_once(self) -> int:
        digest_cutoff = datetime.now(timezone.utc) - timedelta(seconds=self._settings.digest_interval_seconds)
        async with self._session_maker() as session:
            digests = await repository.list_pending_digests(session, digest_cutoff)

        for digest in digests:
            await self._dispatch(digest)
        return len(digests)

    async def _dispatch(self, digest: TopicDigest) -> None:
        async with self._session_maker() as session:
            subscribers = await repository.count_subscribers_capped(
                session, digest.topic_id, self._settings.cold_topic_max_subscribers
            )

        if subscribers <= self._settings.cold_topic_max_subscribers:
            spec = PushPageSpec(
                topic_id=digest.topic_id,
                topic_name=digest.topic_name,
                post_id_from=digest.post_id_from,
                post_id_to=digest.post_id_to,
                article_count=digest.article_count,
                user_id_from=None,
                user_id_to=None,
                cursor_checkpoint=None,
            )
            sent = await self._dispatcher.deliver_page(spec)
            logger.info("cold topic digest topic=%s subscribers=%s sent=%s", digest.topic_id, subscribers, sent)
        else:
            async with self._session_maker() as session:
                ranges = await repository.subscriber_page_ranges(
                    session, digest.topic_id, self._settings.subscriber_page_size
                )
                await repository.enqueue_push_page_jobs(session, digest, ranges)
                await session.commit()
            logger.info("hot topic digest topic=%s pages=%s", digest.topic_id, len(ranges))

        async with self._session_maker() as session:
            await repository.advance_digest_state(session, digest.topic_id, digest.post_id_to)
            await session.commit()


class PushWorker:
    def __init__(
        self,
        settings: PushSettings,
        session_maker: async_sessionmaker[AsyncSession],
        dispatcher: DigestDispatcher,
        index: int,
    ) -> None:
        self._settings = settings
        self._session_maker = session_maker
        self._dispatcher = dispatcher
        self._worker_id = f"push-{index}-{uuid.uuid4().hex[:8]}"

    async def run_forever(self) -> None:
        await run_loop(self._worker_id, self._settings.worker_idle_interval_seconds, self.run_once)

    async def run_once(self) -> int:
        async with self._session_maker() as session:
            jobs = await repository.claim_push_page_jobs(session, self._worker_id, 1, self._settings.lease_seconds)
            await session.commit()
        if not jobs:
            return 0

        for job in jobs:

            async def checkpoint(cursor: str, job_id: int = job.job_id) -> None:
                async with self._session_maker() as session:
                    await repository.checkpoint_push_page_job(session, job_id, cursor)
                    await session.commit()

            try:
                sent = await self._dispatcher.deliver_page(job.spec, checkpoint)
                async with self._session_maker() as session:
                    await repository.complete_push_page_job(session, job.job_id)
                    await session.commit()
                logger.info("push page done job_id=%s topic=%s sent=%s", job.job_id, job.spec.topic_id, sent)
            except Exception as exc:
                logger.exception("push page failed job_id=%s", job.job_id)
                async with self._session_maker() as session:
                    await repository.fail_push_page_job(
                        session,
                        job,
                        repr(exc),
                        self._settings.max_attempts,
                        self._settings.retry_backoff_seconds,
                    )
                    await session.commit()
        return len(jobs)
