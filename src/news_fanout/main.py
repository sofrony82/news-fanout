import argparse
import asyncio
import logging
import os
import sys

import uvicorn

from news_fanout.app import create_app
from news_fanout.config import Role, get_app_settings
from news_fanout.db import connect
from news_fanout.logging_setup import configure_logging
from news_fanout.migrations import apply_schema


async def _migrate() -> None:
    settings = get_app_settings()
    engine, _ = connect(settings.database)
    try:
        await apply_schema(engine)
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="News ingestion, classification and topic notification service")
    parser.add_argument(
        "command",
        nargs="?",
        default="serve",
        choices=["serve", "migrate"],
        help="serve: run the HTTP API and the role's background workers. migrate: apply schema.sql and exit.",
    )
    parser.add_argument("--role", choices=[role.value for role in Role], default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if args.role is not None:
        os.environ["NEWS_FANOUT_ROLE"] = args.role

    configure_logging()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    logger = logging.getLogger(__name__)
    settings = get_app_settings()

    if args.command == "migrate":
        logger.info("applying schema")
        asyncio.run(_migrate())
        return 0

    logger.info("starting news-fanout role=%s", settings.role.value)
    uvicorn.run(
        create_app(settings),
        host=args.host or settings.server.host,
        port=args.port or settings.server.port,
        # Logging is configured above; uvicorn must not replace it.
        log_config=None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
