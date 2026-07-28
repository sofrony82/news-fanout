from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from news_fanout.config import DatabaseSettings


def connect(settings: DatabaseSettings) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    database_url = URL.create(
        "postgresql+asyncpg",
        username=settings.user,
        password=settings.password.get_secret_value(),
        host=settings.host,
        port=settings.port,
        database=settings.database,
    )
    engine = create_async_engine(
        database_url.render_as_string(hide_password=False),
        echo=settings.enable_debug_logging,
        pool_size=settings.pool_size,
        max_overflow=settings.pool_size,
        pool_pre_ping=True,
        pool_use_lifo=True,
    )
    session_maker = async_sessionmaker(bind=engine, autocommit=False, autoflush=False, class_=AsyncSession)
    return engine, session_maker
