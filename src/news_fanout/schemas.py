from datetime import datetime
from enum import StrEnum
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Index, Integer, Text, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class Topic(Base):
    __tablename__ = "topics"

    topic_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)


class Source(Base):
    __tablename__ = "sources"

    source_id: Mapped[str] = mapped_column(Text, primary_key=True)
    page_id_from: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    poll_interval_seconds: Mapped[int] = mapped_column(Integer, server_default=text("60"))
    last_polled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


class Article(Base):
    __tablename__ = "articles"

    article_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(Text, ForeignKey("sources.source_id"))
    external_id: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(Text)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


class ArticleBody(Base):
    __tablename__ = "article_bodies"

    article_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("articles.article_id"), primary_key=True)
    body: Mapped[str] = mapped_column(Text)


class ClassifyJob(Base):
    __tablename__ = "classify_jobs"
    __table_args__ = (Index("classify_jobs_claim_idx", "status", "run_after"),)

    job_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    article_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("articles.article_id"), unique=True)
    status: Mapped[str] = mapped_column(Text, server_default=text("'queued'"))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    lease_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    leased_by: Mapped[Optional[str]] = mapped_column(Text)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


class ArticleByTopic(Base):
    __tablename__ = "articles_by_topic"

    topic_id: Mapped[str] = mapped_column(Text, ForeignKey("topics.topic_id"), primary_key=True)
    post_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        server_default=text("nextval('article_post_id_seq')"),
    )
    article_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("articles.article_id"))
    title: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(Text)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    confidence: Mapped[float] = mapped_column(Float)


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    device_token: Mapped[Optional[str]] = mapped_column(Text)
    last_active_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (Index("subscriptions_by_user_idx", "user_id", "topic_id"),)

    topic_id: Mapped[str] = mapped_column(Text, ForeignKey("topics.topic_id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(Text, ForeignKey("users.user_id"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


class FeedOffset(Base):
    __tablename__ = "feed_offsets"

    user_id: Mapped[str] = mapped_column(Text, ForeignKey("users.user_id"), primary_key=True)
    topic_id: Mapped[str] = mapped_column(Text, ForeignKey("topics.topic_id"), primary_key=True)
    last_seen_post_id: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


class TopicDigestState(Base):
    __tablename__ = "topic_digest_state"

    topic_id: Mapped[str] = mapped_column(Text, ForeignKey("topics.topic_id"), primary_key=True)
    last_pushed_post_id: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    last_pushed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


class PushPageJob(Base):
    __tablename__ = "push_page_jobs"
    __table_args__ = (Index("push_page_jobs_claim_idx", "status", "run_after"),)

    job_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    topic_id: Mapped[str] = mapped_column(Text, ForeignKey("topics.topic_id"))
    post_id_from: Mapped[int] = mapped_column(BigInteger)
    post_id_to: Mapped[int] = mapped_column(BigInteger)
    article_count: Mapped[int] = mapped_column(Integer)
    user_id_from: Mapped[Optional[str]] = mapped_column(Text)
    user_id_to: Mapped[Optional[str]] = mapped_column(Text)
    cursor_checkpoint: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'queued'"))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    lease_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    leased_by: Mapped[Optional[str]] = mapped_column(Text)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
