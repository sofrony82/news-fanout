from datetime import datetime

from pydantic import BaseModel, Field


class RawArticle(BaseModel):
    external_id: str
    title: str
    source_url: str
    published_at: datetime


class ArticlePage(BaseModel):
    page_id_from: int
    page_id_to: int
    articles: list[RawArticle]


class TopicAssignment(BaseModel):
    topic_id: str
    confidence: float


class PushMessage(BaseModel):
    topic_id: str
    topic_name: str
    article_count: int
    post_id_to: int
    title: str
    body: str


class PushResult(BaseModel):
    sent: int
    invalid_tokens: list[str]


class DueSource(BaseModel):
    source_id: str
    page_id_from: int


class ClaimedClassifyJob(BaseModel):
    job_id: int
    article_id: int
    attempts: int


class ArticleForClassification(BaseModel):
    article_id: int
    title: str
    source_url: str
    published_at: datetime
    body: str


class TopicDigest(BaseModel):
    topic_id: str
    topic_name: str
    post_id_from: int
    post_id_to: int
    article_count: int


class UserRange(BaseModel):
    user_id_from: str | None
    user_id_to: str | None


class PushPageSpec(BaseModel):
    topic_id: str
    topic_name: str
    post_id_from: int
    post_id_to: int
    article_count: int
    user_id_from: str | None
    user_id_to: str | None
    cursor_checkpoint: str | None


class ClaimedPushPageJob(BaseModel):
    job_id: int
    attempts: int
    spec: PushPageSpec


class PushTarget(BaseModel):
    user_id: str
    device_token: str


class ArticleItem(BaseModel):
    topic_id: str
    post_id: int
    article_id: int
    title: str
    source_url: str
    published_at: datetime


class SearchResponse(BaseModel):
    articles: list[ArticleItem]
    next_cursor: int | None


class FeedResponse(BaseModel):
    articles: list[ArticleItem]
    watermarks: dict[str, int]


class FeedAckRequest(BaseModel):
    watermarks: dict[str, int] = Field(default_factory=dict)


class TopicsRequest(BaseModel):
    topics: list[str]


class SubscriptionsResponse(BaseModel):
    topics: list[str]


class DeviceRequest(BaseModel):
    device_token: str


class PipelineStats(BaseModel):
    topics: int
    sources: int
    articles: int
    article_topics: int
    users: int
    users_with_device_token: int
    subscriptions: int
    feed_offsets: int
    max_post_id: int
    classify_jobs: dict[str, int]
    push_page_jobs: dict[str, int]
    pushed_topics: dict[str, int]


class ReadyResponse(BaseModel):
    ready: bool
    database: bool
    redis: bool
