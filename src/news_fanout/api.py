from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from news_fanout import repository
from news_fanout.config import AppSettings
from news_fanout.models import (
    DeviceRequest,
    FeedAckRequest,
    FeedResponse,
    SearchResponse,
    SubscriptionsResponse,
    TopicsRequest,
)

health_router = APIRouter(tags=["health"])
v1_router = APIRouter(tags=["v1"], prefix="/v1")


@health_router.get("/healthz", status_code=status.HTTP_200_OK)
async def health_check() -> Response:
    return Response()


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    session_maker: async_sessionmaker[AsyncSession] = request.app.state.session_maker
    async with session_maker() as session:
        yield session


def get_settings(request: Request) -> AppSettings:
    settings: AppSettings = request.app.state.settings
    return settings


SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[AppSettings, Depends(get_settings)]


async def get_current_user(
    session: SessionDep,
    x_user_id: Annotated[str | None, Header()] = None,
) -> str:
    if not x_user_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="X-User-Id header is required")
    await repository.touch_user(session, x_user_id)
    await session.commit()
    return x_user_id


UserDep = Annotated[str, Depends(get_current_user)]


async def _validate_topics(session: AsyncSession, topic_ids: list[str]) -> list[str]:
    known = await repository.filter_known_topics(session, topic_ids)
    unknown = sorted(set(topic_ids) - known)
    if unknown:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"unknown topics: {unknown}")
    return sorted(known)


@v1_router.get("/topics", response_model=list[str])
async def list_topics(session: SessionDep) -> list[str]:
    return await repository.list_topic_ids(session)


@v1_router.post("/devices", status_code=status.HTTP_204_NO_CONTENT)
async def register_device(request: DeviceRequest, session: SessionDep, user_id: UserDep) -> Response:
    await repository.set_device_token(session, user_id, request.device_token)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@v1_router.post("/subscribe", response_model=SubscriptionsResponse)
async def subscribe(request: TopicsRequest, session: SessionDep, user_id: UserDep) -> SubscriptionsResponse:
    topic_ids = await _validate_topics(session, request.topics)
    await repository.add_subscriptions(session, user_id, topic_ids)
    await session.commit()
    return SubscriptionsResponse(topics=await repository.list_subscriptions(session, user_id))


@v1_router.post("/unsubscribe", response_model=SubscriptionsResponse)
async def unsubscribe(request: TopicsRequest, session: SessionDep, user_id: UserDep) -> SubscriptionsResponse:
    await repository.remove_subscriptions(session, user_id, request.topics)
    await session.commit()
    return SubscriptionsResponse(topics=await repository.list_subscriptions(session, user_id))


@v1_router.get("/subscriptions", response_model=SubscriptionsResponse)
async def get_subscriptions(session: SessionDep, user_id: UserDep) -> SubscriptionsResponse:
    return SubscriptionsResponse(topics=await repository.list_subscriptions(session, user_id))


@v1_router.get("/search", response_model=SearchResponse)
async def search(
    session: SessionDep,
    settings: SettingsDep,
    user_id: UserDep,
    topics: Annotated[list[str], Query()],
    cursor: int | None = None,
    limit: int | None = None,
) -> SearchResponse:
    page_size = min(limit or settings.search.default_page_size, settings.search.max_page_size)
    topic_ids = await _validate_topics(session, topics)
    articles = await repository.search_articles(session, topic_ids, cursor, page_size)
    next_cursor = articles[-1].post_id if len(articles) == page_size else None
    return SearchResponse(articles=articles, next_cursor=next_cursor)


@v1_router.get("/feed", response_model=FeedResponse)
async def read_feed(
    session: SessionDep,
    settings: SettingsDep,
    user_id: UserDep,
    limit: int | None = None,
) -> FeedResponse:
    page_size = min(limit or settings.search.default_page_size, settings.search.max_page_size)
    articles = await repository.read_unread_feed(session, user_id, page_size)
    watermarks: dict[str, int] = {}
    for article in articles:
        watermarks[article.topic_id] = max(watermarks.get(article.topic_id, 0), article.post_id)
    return FeedResponse(articles=articles, watermarks=watermarks)


@v1_router.post("/feed/ack", status_code=status.HTTP_204_NO_CONTENT)
async def ack_feed(request: FeedAckRequest, session: SessionDep, user_id: UserDep) -> Response:
    await repository.ack_feed(session, user_id, request.watermarks)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
