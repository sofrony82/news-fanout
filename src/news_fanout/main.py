import argparse
import logging
import os
import sys

import uvicorn
from logging_helpers import configure_logging

from news_fanout.app import create_app
from news_fanout.config import Role, get_app_settings


def main() -> int:
    parser = argparse.ArgumentParser(description="News ingestion, classification and topic notification service")
    parser.add_argument("--role", choices=[role.value for role in Role], default=None)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if args.role is not None:
        os.environ["NEWS_FANOUT_ROLE"] = args.role

    configure_logging()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    settings = get_app_settings()
    logging.getLogger(__name__).info("starting news-fanout role=%s", settings.role.value)
    uvicorn.run(create_app(settings), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
