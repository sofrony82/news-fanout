from collections.abc import Sequence
from typing import Any

import redis.asyncio as redis

from news_fanout.config import RedisSettings


def create_redis_client(settings: RedisSettings) -> redis.Redis:
    return redis.Redis(
        host=settings.host,
        port=settings.port,
        db=settings.db,
        password=settings.password.get_secret_value() if settings.password is not None else None,
        socket_timeout=settings.socket_timeout,
        max_connections=settings.pool_size,
        decode_responses=True,
    )


class PushDedupStore:
    def __init__(self, client: redis.Redis, key_prefix: str, ttl_seconds: int) -> None:
        self._client = client
        self._key_prefix = key_prefix
        self._ttl_seconds = ttl_seconds

    async def claim(self, user_ids: Sequence[str], dedup_key: str) -> set[str]:
        if not user_ids:
            return set()
        pipeline = self._client.pipeline(transaction=False)
        for user_id in user_ids:
            pipeline.set(self._key(dedup_key, user_id), "1", nx=True, ex=self._ttl_seconds)
        results: list[Any] = await pipeline.execute()
        return {user_id for user_id, acquired in zip(user_ids, results, strict=True) if acquired}

    async def release(self, user_ids: Sequence[str], dedup_key: str) -> None:
        """Drop claims so a failed batch can be retried.

        Without this, a send that raises after `claim` succeeded would leave the
        markers in place and the retry would silently skip those users until the
        TTL expired.
        """
        if not user_ids:
            return
        await self._client.delete(*(self._key(dedup_key, user_id) for user_id in user_ids))

    async def ping(self) -> bool:
        return bool(await self._client.ping())

    def _key(self, dedup_key: str, user_id: str) -> str:
        return f"{self._key_prefix}:{dedup_key}:{user_id}"

    async def close(self) -> None:
        await self._client.aclose()
