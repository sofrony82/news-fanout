from enum import StrEnum
from functools import lru_cache

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Role(StrEnum):
    API = "api"
    INGEST = "ingest"
    CLASSIFIER = "classifier"
    PUSH = "push"
    ALL = "all"


class DatabaseSettings(BaseModel):
    host: str = "localhost"
    port: int = 5432
    user: str = "postgres"
    password: SecretStr = SecretStr("postgres")
    database: str = "news_fanout"
    pool_size: int = 10
    enable_debug_logging: bool = False


class RedisSettings(BaseModel):
    host: str = "localhost"
    port: int = 6379
    db: int = 0
    password: SecretStr | None = None
    socket_timeout: float = 1.0
    pool_size: int = 35
    key_prefix: str = "news-fanout:push-dedup"


class IngestSettings(BaseModel):
    idle_interval_seconds: float = 5.0
    sources_per_cycle: int = 10
    articles_per_page: int = 25


class ClassifierSettings(BaseModel):
    idle_interval_seconds: float = 1.0
    batch_size: int = 20
    lease_seconds: float = 60.0
    max_attempts: int = 5
    retry_backoff_seconds: float = 10.0
    min_confidence: float = 0.5
    max_topics_per_article: int = 2


class PushSettings(BaseModel):
    digest_interval_seconds: float = 30.0
    coordinator_idle_interval_seconds: float = 5.0
    worker_idle_interval_seconds: float = 1.0
    worker_count: int = 2
    subscriber_page_size: int = 10_000
    cold_topic_max_subscribers: int = 500
    multicast_batch_size: int = 500
    active_user_window_days: int = 30
    lease_seconds: float = 120.0
    max_attempts: int = 5
    retry_backoff_seconds: float = 15.0
    sends_per_second: float = 2000.0
    dedup_ttl_seconds: int = 86_400


class SearchSettings(BaseModel):
    default_page_size: int = 50
    max_page_size: int = 200


class AppSettings(BaseSettings):
    role: Role = Role.ALL
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    ingest: IngestSettings = Field(default_factory=IngestSettings)
    classifier: ClassifierSettings = Field(default_factory=ClassifierSettings)
    push: PushSettings = Field(default_factory=PushSettings)
    search: SearchSettings = Field(default_factory=SearchSettings)

    model_config = SettingsConfigDict(
        env_prefix="NEWS_FANOUT_",
        env_file=".env",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_app_settings() -> AppSettings:
    return AppSettings()
