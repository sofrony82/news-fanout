from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Row, and_, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from news_fanout.models import (
    ArticleForClassification,
    ArticleItem,
    ClaimedClassifyJob,
    ClaimedPushPageJob,
    DueSource,
    PipelineStats,
    PushPageSpec,
    PushTarget,
    RawArticle,
    TopicAssignment,
    TopicDigest,
    UserRange,
)
from news_fanout.schemas import (
    Article,
    ArticleBody,
    ArticleByTopic,
    ClassifyJob,
    FeedOffset,
    JobStatus,
    PushPageJob,
    Source,
    Subscription,
    Topic,
    TopicDigestState,
    User,
)


_SOURCE_IS_DUE = (
    "sources.last_polled_at IS NULL "
    "OR sources.last_polled_at + make_interval(secs => sources.poll_interval_seconds) <= now()"
)

ArticleRow = Row[tuple[str, int, int, str, str, datetime]]


async def list_topic_ids(session: AsyncSession) -> list[str]:
    result = await session.execute(select(Topic.topic_id).order_by(Topic.topic_id))
    return list(result.scalars().all())


async def filter_known_topics(session: AsyncSession, topic_ids: Sequence[str]) -> set[str]:
    if not topic_ids:
        return set()
    result = await session.execute(select(Topic.topic_id).where(Topic.topic_id.in_(topic_ids)))
    return set(result.scalars().all())


async def claim_due_sources(session: AsyncSession, limit: int) -> list[DueSource]:
    candidates = (
        select(Source.source_id)
        .where(text(_SOURCE_IS_DUE))
        .limit(limit)
        .with_for_update(skip_locked=True)
        .cte("due_sources")
    )
    stmt = (
        update(Source)
        .where(Source.source_id == candidates.c.source_id)
        .values(last_polled_at=func.now(), updated_at=func.now())
        .returning(Source.source_id, Source.page_id_from)
    )
    result = await session.execute(stmt)
    return [DueSource(source_id=row.source_id, page_id_from=row.page_id_from) for row in result.all()]


async def store_articles(
    session: AsyncSession,
    source_id: str,
    articles: Sequence[tuple[RawArticle, str]],
) -> list[int]:
    if not articles:
        return []
    article_rows = [
        {
            "source_id": source_id,
            "external_id": article.external_id,
            "title": article.title,
            "source_url": article.source_url,
            "published_at": article.published_at,
        }
        for article, _ in articles
    ]
    insert_articles = (
        pg_insert(Article)
        .values(article_rows)
        .on_conflict_do_nothing(constraint="articles_source_external_uniq")
        .returning(Article.article_id, Article.external_id)
    )
    inserted = (await session.execute(insert_articles)).all()
    if not inserted:
        return []

    body_by_external_id = {article.external_id: body for article, body in articles}
    body_rows = [{"article_id": row.article_id, "body": body_by_external_id[row.external_id]} for row in inserted]
    await session.execute(pg_insert(ArticleBody).values(body_rows).on_conflict_do_nothing())

    job_rows = [{"article_id": row.article_id} for row in inserted]
    await session.execute(pg_insert(ClassifyJob).values(job_rows).on_conflict_do_nothing())

    return [row.article_id for row in inserted]


async def advance_source_cursor(session: AsyncSession, source_id: str, page_id_to: int) -> None:
    await session.execute(
        update(Source)
        .where(Source.source_id == source_id, Source.page_id_from < page_id_to)
        .values(page_id_from=page_id_to, updated_at=func.now())
    )


async def claim_classify_jobs(
    session: AsyncSession,
    worker_id: str,
    limit: int,
    lease_seconds: float,
) -> list[ClaimedClassifyJob]:
    candidates = (
        select(ClassifyJob.job_id)
        .where(
            ClassifyJob.run_after <= func.now(),
            or_(
                ClassifyJob.status == JobStatus.QUEUED,
                and_(ClassifyJob.status == JobStatus.RUNNING, ClassifyJob.lease_until < func.now()),
            ),
        )
        .order_by(ClassifyJob.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .cte("classify_candidates")
    )
    stmt = (
        update(ClassifyJob)
        .where(ClassifyJob.job_id == candidates.c.job_id)
        .values(
            status=JobStatus.RUNNING,
            leased_by=worker_id,
            lease_until=func.now() + timedelta(seconds=lease_seconds),
            attempts=ClassifyJob.attempts + 1,
            updated_at=func.now(),
        )
        .returning(ClassifyJob.job_id, ClassifyJob.article_id, ClassifyJob.attempts)
    )
    result = await session.execute(stmt)
    return [
        ClaimedClassifyJob(job_id=row.job_id, article_id=row.article_id, attempts=row.attempts) for row in result.all()
    ]


async def load_article_for_classification(session: AsyncSession, article_id: int) -> ArticleForClassification | None:
    stmt = (
        select(Article.article_id, Article.title, Article.source_url, Article.published_at, ArticleBody.body)
        .join(ArticleBody, ArticleBody.article_id == Article.article_id)
        .where(Article.article_id == article_id)
    )
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        return None
    return ArticleForClassification(
        article_id=row.article_id,
        title=row.title,
        source_url=row.source_url,
        published_at=row.published_at,
        body=row.body,
    )


async def publish_article_topics(
    session: AsyncSession,
    article: ArticleForClassification,
    assignments: Sequence[TopicAssignment],
) -> list[int]:
    post_ids: list[int] = []
    for assignment in sorted(assignments, key=lambda item: item.topic_id):
        await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(assignment.topic_id))))
        stmt = (
            pg_insert(ArticleByTopic)
            .values(
                topic_id=assignment.topic_id,
                article_id=article.article_id,
                title=article.title,
                source_url=article.source_url,
                published_at=article.published_at,
                confidence=assignment.confidence,
            )
            .on_conflict_do_nothing(constraint="articles_by_topic_article_uniq")
            .returning(ArticleByTopic.post_id)
        )
        post_id = (await session.execute(stmt)).scalar_one_or_none()
        if post_id is not None:
            post_ids.append(post_id)
    return post_ids


async def complete_classify_job(session: AsyncSession, job_id: int) -> None:
    await session.execute(
        update(ClassifyJob)
        .where(ClassifyJob.job_id == job_id)
        .values(status=JobStatus.DONE, lease_until=None, last_error=None, updated_at=func.now())
    )


async def fail_classify_job(
    session: AsyncSession,
    job: ClaimedClassifyJob,
    error: str,
    max_attempts: int,
    retry_backoff_seconds: float,
) -> None:
    exhausted = job.attempts >= max_attempts
    await session.execute(
        update(ClassifyJob)
        .where(ClassifyJob.job_id == job.job_id)
        .values(
            status=JobStatus.FAILED if exhausted else JobStatus.QUEUED,
            run_after=func.now() + timedelta(seconds=retry_backoff_seconds * job.attempts),
            lease_until=None,
            last_error=error[:2000],
            updated_at=func.now(),
        )
    )


async def touch_user(session: AsyncSession, user_id: str) -> None:
    stmt = pg_insert(User).values(user_id=user_id)
    await session.execute(
        stmt.on_conflict_do_update(index_elements=[User.user_id], set_={"last_active_at": func.now()})
    )


async def set_device_token(session: AsyncSession, user_id: str, device_token: str) -> None:
    await session.execute(update(User).where(User.user_id == user_id).values(device_token=device_token))


async def add_subscriptions(session: AsyncSession, user_id: str, topic_ids: Sequence[str]) -> None:
    if not topic_ids:
        return
    rows = [{"user_id": user_id, "topic_id": topic_id} for topic_id in topic_ids]
    await session.execute(pg_insert(Subscription).values(rows).on_conflict_do_nothing())


async def remove_subscriptions(session: AsyncSession, user_id: str, topic_ids: Sequence[str]) -> None:
    if not topic_ids:
        return
    await session.execute(
        delete(Subscription).where(Subscription.user_id == user_id, Subscription.topic_id.in_(topic_ids))
    )


async def list_subscriptions(session: AsyncSession, user_id: str) -> list[str]:
    result = await session.execute(
        select(Subscription.topic_id).where(Subscription.user_id == user_id).order_by(Subscription.topic_id)
    )
    return list(result.scalars().all())


def _to_article_items(rows: Sequence[ArticleRow]) -> list[ArticleItem]:
    return [
        ArticleItem(
            topic_id=topic_id,
            post_id=post_id,
            article_id=article_id,
            title=title,
            source_url=source_url,
            published_at=published_at,
        )
        for topic_id, post_id, article_id, title, source_url, published_at in rows
    ]


async def search_articles(
    session: AsyncSession,
    topic_ids: Sequence[str],
    cursor: int | None,
    limit: int,
) -> list[ArticleItem]:
    if not topic_ids:
        return []
    stmt = select(
        ArticleByTopic.topic_id,
        ArticleByTopic.post_id,
        ArticleByTopic.article_id,
        ArticleByTopic.title,
        ArticleByTopic.source_url,
        ArticleByTopic.published_at,
    ).where(ArticleByTopic.topic_id.in_(topic_ids))
    if cursor is not None:
        stmt = stmt.where(ArticleByTopic.post_id < cursor)
    stmt = stmt.order_by(ArticleByTopic.post_id.desc()).limit(limit)
    return _to_article_items((await session.execute(stmt)).all())


async def read_unread_feed(session: AsyncSession, user_id: str, limit: int) -> list[ArticleItem]:
    stmt = (
        select(
            ArticleByTopic.topic_id,
            ArticleByTopic.post_id,
            ArticleByTopic.article_id,
            ArticleByTopic.title,
            ArticleByTopic.source_url,
            ArticleByTopic.published_at,
        )
        .join(Subscription, Subscription.topic_id == ArticleByTopic.topic_id)
        .outerjoin(
            FeedOffset,
            and_(FeedOffset.user_id == Subscription.user_id, FeedOffset.topic_id == Subscription.topic_id),
        )
        .where(
            Subscription.user_id == user_id,
            ArticleByTopic.post_id > func.coalesce(FeedOffset.last_seen_post_id, 0),
        )
        .order_by(ArticleByTopic.post_id.desc())
        .limit(limit)
    )
    return _to_article_items((await session.execute(stmt)).all())


async def ack_feed(session: AsyncSession, user_id: str, watermarks: dict[str, int]) -> None:
    if not watermarks:
        return
    rows = [
        {"user_id": user_id, "topic_id": topic_id, "last_seen_post_id": post_id}
        for topic_id, post_id in watermarks.items()
    ]
    stmt = pg_insert(FeedOffset).values(rows)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[FeedOffset.user_id, FeedOffset.topic_id],
            set_={
                "last_seen_post_id": func.greatest(FeedOffset.last_seen_post_id, stmt.excluded.last_seen_post_id),
                "updated_at": func.now(),
            },
        )
    )


async def list_pending_digests(session: AsyncSession, digest_cutoff: datetime) -> list[TopicDigest]:
    last_pushed_post_id = func.coalesce(TopicDigestState.last_pushed_post_id, 0)
    stmt = (
        select(
            Topic.topic_id,
            Topic.name,
            last_pushed_post_id.label("post_id_from"),
            func.max(ArticleByTopic.post_id).label("post_id_to"),
            func.count().label("article_count"),
        )
        .join(ArticleByTopic, ArticleByTopic.topic_id == Topic.topic_id)
        .outerjoin(TopicDigestState, TopicDigestState.topic_id == Topic.topic_id)
        .where(
            ArticleByTopic.post_id > last_pushed_post_id,
            or_(TopicDigestState.last_pushed_at.is_(None), TopicDigestState.last_pushed_at <= digest_cutoff),
        )
        .group_by(Topic.topic_id, Topic.name, TopicDigestState.last_pushed_post_id)
    )
    result = await session.execute(stmt)
    return [
        TopicDigest(
            topic_id=row.topic_id,
            topic_name=row.name,
            post_id_from=row.post_id_from,
            post_id_to=row.post_id_to,
            article_count=row.article_count,
        )
        for row in result.all()
    ]


async def count_subscribers_capped(session: AsyncSession, topic_id: str, cap: int) -> int:
    capped = select(Subscription.user_id).where(Subscription.topic_id == topic_id).limit(cap + 1).subquery()
    return (await session.execute(select(func.count()).select_from(capped))).scalar_one()


async def subscriber_page_ranges(session: AsyncSession, topic_id: str, page_size: int) -> list[UserRange]:
    ranked = (
        select(
            Subscription.user_id,
            func.row_number().over(order_by=Subscription.user_id).label("rn"),
        )
        .where(Subscription.topic_id == topic_id)
        .cte("ranked_subscribers")
    )
    stmt = select(ranked.c.user_id).where(ranked.c.rn % page_size == 0).order_by(ranked.c.user_id)
    boundaries = list((await session.execute(stmt)).scalars().all())

    ranges: list[UserRange] = []
    previous: str | None = None
    for boundary in boundaries:
        ranges.append(UserRange(user_id_from=previous, user_id_to=boundary))
        previous = boundary
    ranges.append(UserRange(user_id_from=previous, user_id_to=None))
    return ranges


async def enqueue_push_page_jobs(session: AsyncSession, digest: TopicDigest, ranges: Sequence[UserRange]) -> None:
    if not ranges:
        return
    rows = [
        {
            "topic_id": digest.topic_id,
            "post_id_from": digest.post_id_from,
            "post_id_to": digest.post_id_to,
            "article_count": digest.article_count,
            "user_id_from": user_range.user_id_from,
            "user_id_to": user_range.user_id_to,
        }
        for user_range in ranges
    ]
    await session.execute(pg_insert(PushPageJob).values(rows))


async def advance_digest_state(session: AsyncSession, topic_id: str, post_id_to: int) -> None:
    stmt = pg_insert(TopicDigestState).values(topic_id=topic_id, last_pushed_post_id=post_id_to)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[TopicDigestState.topic_id],
            set_={
                "last_pushed_post_id": func.greatest(
                    TopicDigestState.last_pushed_post_id, stmt.excluded.last_pushed_post_id
                ),
                "last_pushed_at": func.now(),
            },
        )
    )


async def claim_push_page_jobs(
    session: AsyncSession,
    worker_id: str,
    limit: int,
    lease_seconds: float,
) -> list[ClaimedPushPageJob]:
    candidates = (
        select(PushPageJob.job_id)
        .where(
            PushPageJob.run_after <= func.now(),
            or_(
                PushPageJob.status == JobStatus.QUEUED,
                and_(PushPageJob.status == JobStatus.RUNNING, PushPageJob.lease_until < func.now()),
            ),
        )
        .order_by(PushPageJob.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .cte("push_candidates")
    )
    claimed = (
        update(PushPageJob)
        .where(PushPageJob.job_id == candidates.c.job_id)
        .values(
            status=JobStatus.RUNNING,
            leased_by=worker_id,
            lease_until=func.now() + timedelta(seconds=lease_seconds),
            attempts=PushPageJob.attempts + 1,
            updated_at=func.now(),
        )
        .returning(PushPageJob.job_id)
    )
    job_ids = list((await session.execute(claimed)).scalars().all())
    if not job_ids:
        return []

    stmt = (
        select(
            PushPageJob.job_id,
            PushPageJob.attempts,
            PushPageJob.topic_id,
            Topic.name,
            PushPageJob.post_id_from,
            PushPageJob.post_id_to,
            PushPageJob.article_count,
            PushPageJob.user_id_from,
            PushPageJob.user_id_to,
            PushPageJob.cursor_checkpoint,
        )
        .join(Topic, Topic.topic_id == PushPageJob.topic_id)
        .where(PushPageJob.job_id.in_(job_ids))
    )
    result = await session.execute(stmt)
    return [
        ClaimedPushPageJob(
            job_id=row.job_id,
            attempts=row.attempts,
            spec=PushPageSpec(
                topic_id=row.topic_id,
                topic_name=row.name,
                post_id_from=row.post_id_from,
                post_id_to=row.post_id_to,
                article_count=row.article_count,
                user_id_from=row.user_id_from,
                user_id_to=row.user_id_to,
                cursor_checkpoint=row.cursor_checkpoint,
            ),
        )
        for row in result.all()
    ]


async def load_push_targets(
    session: AsyncSession,
    topic_id: str,
    user_id_after: str | None,
    user_id_to: str | None,
    active_since: datetime,
    limit: int,
) -> list[PushTarget]:
    stmt = (
        select(Subscription.user_id, User.device_token)
        .join(User, User.user_id == Subscription.user_id)
        .where(
            Subscription.topic_id == topic_id,
            User.device_token.is_not(None),
            User.last_active_at >= active_since,
        )
        .order_by(Subscription.user_id)
        .limit(limit)
    )
    if user_id_after is not None:
        stmt = stmt.where(Subscription.user_id > user_id_after)
    if user_id_to is not None:
        stmt = stmt.where(Subscription.user_id <= user_id_to)
    result = await session.execute(stmt)
    return [PushTarget(user_id=row.user_id, device_token=row.device_token) for row in result.all()]


async def checkpoint_push_page_job(session: AsyncSession, job_id: int, cursor: str) -> None:
    await session.execute(
        update(PushPageJob).where(PushPageJob.job_id == job_id).values(cursor_checkpoint=cursor, updated_at=func.now())
    )


async def complete_push_page_job(session: AsyncSession, job_id: int) -> None:
    await session.execute(
        update(PushPageJob)
        .where(PushPageJob.job_id == job_id)
        .values(status=JobStatus.DONE, lease_until=None, last_error=None, updated_at=func.now())
    )


async def fail_push_page_job(
    session: AsyncSession,
    job: ClaimedPushPageJob,
    error: str,
    max_attempts: int,
    retry_backoff_seconds: float,
) -> None:
    exhausted = job.attempts >= max_attempts
    await session.execute(
        update(PushPageJob)
        .where(PushPageJob.job_id == job.job_id)
        .values(
            status=JobStatus.FAILED if exhausted else JobStatus.QUEUED,
            run_after=func.now() + timedelta(seconds=retry_backoff_seconds * job.attempts),
            lease_until=None,
            last_error=error[:2000],
            updated_at=func.now(),
        )
    )


async def prune_device_tokens(session: AsyncSession, tokens: Sequence[str]) -> None:
    if not tokens:
        return
    await session.execute(update(User).where(User.device_token.in_(tokens)).values(device_token=None))


async def ping(session: AsyncSession) -> None:
    await session.execute(select(1))


async def _count(session: AsyncSession, entity: type, *criteria: Any) -> int:
    stmt = select(func.count()).select_from(entity)
    if criteria:
        stmt = stmt.where(*criteria)
    return (await session.execute(stmt)).scalar_one()


async def _count_by_status(session: AsyncSession, entity: type) -> dict[str, int]:
    stmt = select(entity.status, func.count()).group_by(entity.status)
    return {status: count for status, count in (await session.execute(stmt)).all()}


async def collect_stats(session: AsyncSession) -> PipelineStats:
    """Counters across the whole pipeline.

    Exposed over HTTP so the end-to-end test can assert that ingest, classification
    and the digest push pipeline actually made progress — none of which is visible
    through the user-facing endpoints.
    """
    digest_rows = (
        await session.execute(
            select(TopicDigestState.topic_id, TopicDigestState.last_pushed_post_id).order_by(
                TopicDigestState.topic_id
            )
        )
    ).all()
    max_post_id = (await session.execute(select(func.coalesce(func.max(ArticleByTopic.post_id), 0)))).scalar_one()

    return PipelineStats(
        topics=await _count(session, Topic),
        sources=await _count(session, Source),
        articles=await _count(session, Article),
        article_topics=await _count(session, ArticleByTopic),
        users=await _count(session, User),
        users_with_device_token=await _count(session, User, User.device_token.is_not(None)),
        subscriptions=await _count(session, Subscription),
        feed_offsets=await _count(session, FeedOffset),
        max_post_id=max_post_id,
        classify_jobs=await _count_by_status(session, ClassifyJob),
        push_page_jobs=await _count_by_status(session, PushPageJob),
        pushed_topics={row.topic_id: row.last_pushed_post_id for row in digest_rows},
    )
