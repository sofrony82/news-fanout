import asyncio
import hashlib
import logging
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Protocol

from news_fanout.models import ArticlePage, PushMessage, PushResult, RawArticle, TopicAssignment

logger = logging.getLogger(__name__)

DEAD_TOKEN_SUFFIX = "-dead"


class ArticleSource(Protocol):
    async def get_articles(self, source_id: str, page_id_from: int, limit: int) -> ArticlePage: ...

    async def fetch_body(self, article: RawArticle) -> str: ...


class TopicClassifier(Protocol):
    async def classify(self, title: str, body: str, topic_ids: Sequence[str]) -> list[TopicAssignment]: ...


class PushSender(Protocol):
    async def send_multicast(self, tokens: Sequence[str], message: PushMessage) -> PushResult: ...


class StubArticleSource:
    def __init__(self, articles_per_page: int) -> None:
        self._articles_per_page = articles_per_page

    async def get_articles(self, source_id: str, page_id_from: int, limit: int) -> ArticlePage:
        count = min(limit, self._articles_per_page)
        now = datetime.now(timezone.utc)
        articles = [
            RawArticle(
                external_id=f"{source_id}-{page_id_from + offset}",
                title=f"{source_id} story #{page_id_from + offset}",
                source_url=f"https://stub.invalid/{source_id}/{page_id_from + offset}",
                published_at=now,
            )
            for offset in range(1, count + 1)
        ]
        return ArticlePage(page_id_from=page_id_from, page_id_to=page_id_from + count, articles=articles)

    async def fetch_body(self, article: RawArticle) -> str:
        return f"Stub body for {article.external_id}. {article.title}. " * 5


class StubTopicClassifier:
    def __init__(self, max_topics_per_article: int) -> None:
        self._max_topics_per_article = max_topics_per_article

    async def classify(self, title: str, body: str, topic_ids: Sequence[str]) -> list[TopicAssignment]:
        if not topic_ids:
            return []
        digest = hashlib.blake2b(f"{title}\n{body}".encode(), digest_size=8).digest()
        seed = int.from_bytes(digest, "big")
        assignments: list[TopicAssignment] = []
        chosen: set[str] = set()
        for slot in range(self._max_topics_per_article):
            topic_id = topic_ids[(seed >> (slot * 8)) % len(topic_ids)]
            if topic_id in chosen:
                continue
            chosen.add(topic_id)
            confidence = 0.5 + ((seed >> (32 + slot * 8)) % 50) / 100
            assignments.append(TopicAssignment(topic_id=topic_id, confidence=round(confidence, 2)))
        return assignments


class LoggingPushSender:
    async def send_multicast(self, tokens: Sequence[str], message: PushMessage) -> PushResult:
        invalid = [token for token in tokens if token.endswith(DEAD_TOKEN_SUFFIX)]
        logger.info(
            "push multicast topic=%s post_id_to=%s tokens=%s invalid=%s body=%r",
            message.topic_id,
            message.post_id_to,
            len(tokens),
            len(invalid),
            message.body,
        )
        return PushResult(sent=len(tokens) - len(invalid), invalid_tokens=invalid)


class RateLimiter:
    def __init__(self, rate_per_second: float) -> None:
        self._rate = rate_per_second
        self._allowance = rate_per_second
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, amount: float = 1.0) -> None:
        if self._rate <= 0 or amount <= 0:
            return
        capacity = max(self._rate, amount)
        async with self._lock:
            while True:
                now = time.monotonic()
                self._allowance = min(capacity, self._allowance + (now - self._updated) * self._rate)
                self._updated = now
                if self._allowance >= amount:
                    self._allowance -= amount
                    return
                await asyncio.sleep((amount - self._allowance) / self._rate)
