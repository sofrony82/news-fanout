#!/usr/bin/env python3
"""End-to-end smoke test for news-fanout.

Drives the deployed service through the whole pipeline and asserts observable
behaviour: ingest -> classify -> search/feed -> watermark ack -> digest push.

Stdlib only, so it runs against a local compose stack or the deployed VM with no
install step:

    python3 scripts/e2e_test.py
    python3 scripts/e2e_test.py --base-url http://34.12.34.56

Exit code is 0 only if every check passed.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

DEFAULT_BASE_URL = "http://localhost:8000"


class CheckFailed(Exception):
    pass


class Client:
    def __init__(self, base_url: str, timeout: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        user_id: str | None = None,
    ) -> tuple[int, Any]:
        url = f"{self._base_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"

        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if user_id is not None:
            headers["X-User-Id"] = user_id

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return response.status, _decode(response.read())
        except urllib.error.HTTPError as error:
            return error.code, _decode(error.read())

    def get(self, path: str, **kwargs: Any) -> tuple[int, Any]:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> tuple[int, Any]:
        return self.request("POST", path, **kwargs)


def _decode(raw: bytes) -> Any:
    """Parse a JSON body, falling back to text (/metrics is Prometheus format)."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw.decode(errors="replace")


class Runner:
    """Collects results so one failure does not hide the checks that follow it."""

    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def check(self, name: str, fn: Any) -> Any:
        try:
            detail = fn()
        except Exception as exc:  # noqa: BLE001 - a failed check must not abort the run
            self.failed += 1
            print(f"  [FAIL] {name}: {exc}")
            return None
        self.passed += 1
        print(f"  [ok]   {name}" + (f"  ({detail})" if detail else ""))
        return detail


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


def expect_status(actual: int, expected: int, context: str) -> None:
    expect(actual == expected, f"{context}: expected HTTP {expected}, got {actual}")


def wait_until(predicate: Any, timeout: float, interval: float, description: str) -> Any:
    """Poll until predicate returns a truthy value. Returns it, or raises."""
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
        except Exception:  # noqa: BLE001 - service may still be starting
            last = None
        if last:
            return last
        time.sleep(interval)
    raise CheckFailed(f"timed out after {timeout:.0f}s waiting for {description}")


# --------------------------------------------------------------------------- checks


def check_liveness(client: Client, timeout: float) -> str:
    def probe() -> bool:
        status, _ = client.get("/healthz")
        return status == 200

    wait_until(probe, timeout, 1.0, "/healthz to return 200")
    return "service is live"


def check_readiness(client: Client, timeout: float) -> str:
    def probe() -> dict[str, Any] | None:
        status, payload = client.get("/readyz")
        if status == 200 and isinstance(payload, dict) and payload.get("ready"):
            return payload
        return None

    payload = wait_until(probe, timeout, 1.0, "/readyz to report ready")
    expect(payload["database"] is True, "database not ready")
    expect(payload["redis"] is True, "redis not ready")
    return "database + redis reachable"


def check_topics(client: Client) -> list[str]:
    status, payload = client.get("/v1/topics")
    expect_status(status, 200, "GET /v1/topics")
    expect(isinstance(payload, list) and len(payload) > 0, "topic list is empty")
    expect(payload == sorted(payload), "topics are not sorted")
    return payload


def check_auth_required(client: Client) -> str:
    status, _ = client.get("/v1/subscriptions")
    expect_status(status, 400, "GET /v1/subscriptions without X-User-Id")
    return "missing X-User-Id rejected with 400"


def check_unknown_topic_rejected(client: Client, user_id: str) -> str:
    bogus = f"no-such-topic-{uuid.uuid4().hex[:6]}"
    status, _ = client.post("/v1/subscribe", body={"topics": [bogus]}, user_id=user_id)
    expect_status(status, 400, "POST /v1/subscribe with an unknown topic")
    return "unknown topic rejected with 400"


def check_device_registration(client: Client, user_id: str) -> str:
    token = f"tok-{user_id}"
    status, _ = client.post("/v1/devices", body={"device_token": token}, user_id=user_id)
    expect_status(status, 204, "POST /v1/devices")
    return f"token registered for {user_id}"


def check_subscribe(client: Client, user_id: str, topics: list[str]) -> str:
    status, payload = client.post("/v1/subscribe", body={"topics": topics}, user_id=user_id)
    expect_status(status, 200, "POST /v1/subscribe")
    expect(payload["topics"] == sorted(topics), f"subscriptions {payload['topics']} != {sorted(topics)}")

    status, payload = client.get("/v1/subscriptions", user_id=user_id)
    expect_status(status, 200, "GET /v1/subscriptions")
    expect(payload["topics"] == sorted(topics), "GET /v1/subscriptions disagrees with POST /v1/subscribe")

    # Subscribing twice must not duplicate rows or change the result.
    status, payload = client.post("/v1/subscribe", body={"topics": topics}, user_id=user_id)
    expect_status(status, 200, "POST /v1/subscribe (repeat)")
    expect(payload["topics"] == sorted(topics), "repeat subscribe changed the subscription set")
    return f"{len(topics)} topics, idempotent"


def check_pipeline_produces_articles(client: Client, timeout: float) -> str:
    def probe() -> dict[str, Any] | None:
        status, payload = client.get("/internal/stats")
        if status == 200 and isinstance(payload, dict) and payload.get("article_topics", 0) > 0:
            return payload
        return None

    stats = wait_until(probe, timeout, 2.0, "ingest + classification to publish articles")
    expect(stats["articles"] > 0, "articles were never ingested")
    expect(stats["max_post_id"] > 0, "post_id sequence never advanced")
    failed = stats["classify_jobs"].get("failed", 0)
    expect(failed == 0, f"{failed} classify jobs failed")
    return f"{stats['articles']} articles, {stats['article_topics']} topic assignments"


def check_search(client: Client, user_id: str, topics: list[str]) -> str:
    status, payload = client.get("/v1/search", params={"topics": topics, "limit": 10}, user_id=user_id)
    expect_status(status, 200, "GET /v1/search")
    articles = payload["articles"]
    expect(len(articles) > 0, "search returned no articles")

    post_ids = [article["post_id"] for article in articles]
    expect(post_ids == sorted(post_ids, reverse=True), "search results are not newest-first")
    expect(len(set(post_ids)) == len(post_ids), "search returned duplicate post_ids")
    for article in articles:
        expect(article["topic_id"] in topics, f"search leaked topic {article['topic_id']}")
    return f"{len(articles)} articles, newest-first, topic-filtered"


def check_search_paging(client: Client, user_id: str, topics: list[str]) -> str:
    limit = 5
    status, first = client.get("/v1/search", params={"topics": topics, "limit": limit}, user_id=user_id)
    expect_status(status, 200, "GET /v1/search (page 1)")
    expect(len(first["articles"]) == limit, f"page 1 returned {len(first['articles'])} of {limit}")
    expect(first["next_cursor"] is not None, "page 1 has no next_cursor")

    status, second = client.get(
        "/v1/search",
        params={"topics": topics, "limit": limit, "cursor": first["next_cursor"]},
        user_id=user_id,
    )
    expect_status(status, 200, "GET /v1/search (page 2)")

    first_ids = {article["post_id"] for article in first["articles"]}
    second_ids = {article["post_id"] for article in second["articles"]}
    expect(not (first_ids & second_ids), "pages overlap")
    if second_ids:
        expect(max(second_ids) < min(first_ids), "page 2 is not strictly older than page 1")
    return f"2 pages, {len(first_ids)}+{len(second_ids)} articles, no overlap"


def check_feed_watermark(client: Client, user_id: str) -> str:
    status, feed = client.get("/v1/feed", params={"limit": 20}, user_id=user_id)
    expect_status(status, 200, "GET /v1/feed")
    expect(len(feed["articles"]) > 0, "feed is empty before ack")
    expect(len(feed["watermarks"]) > 0, "feed returned no watermarks")
    seen_ids = {article["post_id"] for article in feed["articles"]}

    status, _ = client.post("/v1/feed/ack", body={"watermarks": feed["watermarks"]}, user_id=user_id)
    expect_status(status, 204, "POST /v1/feed/ack")

    status, after = client.get("/v1/feed", params={"limit": 20}, user_id=user_id)
    expect_status(status, 200, "GET /v1/feed (after ack)")
    still_there = seen_ids & {article["post_id"] for article in after["articles"]}
    expect(not still_there, f"{len(still_there)} acked articles came back in the feed")

    # The watermark must not rewind when an older value is acked again.
    stale = {topic: 1 for topic in feed["watermarks"]}
    status, _ = client.post("/v1/feed/ack", body={"watermarks": stale}, user_id=user_id)
    expect_status(status, 204, "POST /v1/feed/ack (stale watermark)")
    status, replayed = client.get("/v1/feed", params={"limit": 20}, user_id=user_id)
    expect_status(status, 200, "GET /v1/feed (after stale ack)")
    regressed = seen_ids & {article["post_id"] for article in replayed["articles"]}
    expect(not regressed, "a stale ack rewound the watermark")
    return f"{len(seen_ids)} articles acked, watermark monotonic"


def check_unsubscribe(client: Client, user_id: str, topics: list[str]) -> str:
    dropped = topics[0]
    status, payload = client.post("/v1/unsubscribe", body={"topics": [dropped]}, user_id=user_id)
    expect_status(status, 200, "POST /v1/unsubscribe")
    expect(dropped not in payload["topics"], f"{dropped} still subscribed after unsubscribe")
    expect(len(payload["topics"]) == len(topics) - 1, "unsubscribe removed the wrong number of topics")

    status, feed = client.get("/v1/feed", params={"limit": 50}, user_id=user_id)
    expect_status(status, 200, "GET /v1/feed (after unsubscribe)")
    leaked = [a for a in feed["articles"] if a["topic_id"] == dropped]
    expect(not leaked, f"feed still serves {len(leaked)} articles from unsubscribed topic {dropped}")
    return f"removed {dropped}, feed no longer serves it"


def check_digest_push(client: Client, timeout: float) -> str:
    def probe() -> dict[str, Any] | None:
        status, payload = client.get("/internal/stats")
        if status != 200 or not isinstance(payload, dict):
            return None
        pushed = {topic: post_id for topic, post_id in payload["pushed_topics"].items() if post_id > 0}
        return {"pushed": pushed, "jobs": payload["push_page_jobs"]} if pushed else None

    result = wait_until(probe, timeout, 3.0, "the digest push pipeline to advance a topic watermark")
    failed = result["jobs"].get("failed", 0)
    expect(failed == 0, f"{failed} push page jobs failed")
    return f"{len(result['pushed'])} topics pushed"


def check_metrics(client: Client) -> str:
    status, payload = client.get("/metrics")
    expect_status(status, 200, "GET /metrics")
    expect(isinstance(payload, str), "/metrics did not return a text body")
    expect("http_requests_total" in payload, "/metrics is missing http_requests_total")
    return "prometheus endpoint served"


# ---------------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description="End-to-end smoke test for news-fanout")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"service base URL (default {DEFAULT_BASE_URL})")
    parser.add_argument("--timeout", type=float, default=10.0, help="per-request timeout in seconds")
    parser.add_argument("--startup-timeout", type=float, default=120.0, help="how long to wait for readiness")
    parser.add_argument("--pipeline-timeout", type=float, default=120.0, help="how long to wait for articles")
    parser.add_argument("--push-timeout", type=float, default=180.0, help="how long to wait for a digest push")
    parser.add_argument("--skip-push", action="store_true", help="skip the digest push check")
    args = parser.parse_args()

    client = Client(args.base_url, args.timeout)
    runner = Runner()
    user_id = f"e2e-{uuid.uuid4().hex[:10]}"

    print(f"\nnews-fanout end-to-end test\n  target: {args.base_url}\n  user:   {user_id}\n")

    print("health")
    runner.check("liveness /healthz", lambda: check_liveness(client, args.startup_timeout))
    runner.check("readiness /readyz", lambda: check_readiness(client, args.startup_timeout))
    runner.check("metrics /metrics", lambda: check_metrics(client))

    print("\napi contract")
    topics = runner.check("topics listed", lambda: check_topics(client))
    if not topics:
        print("\nno topics available - the schema seed did not run; aborting.")
        return 1
    runner.check("auth required", lambda: check_auth_required(client))
    runner.check("unknown topic rejected", lambda: check_unknown_topic_rejected(client, user_id))

    subscribed = topics[: min(3, len(topics))]
    print("\nsubscriptions")
    runner.check("device registered", lambda: check_device_registration(client, user_id))
    runner.check("subscribe", lambda: check_subscribe(client, user_id, subscribed))

    print("\ningest -> classify")
    runner.check("articles published", lambda: check_pipeline_produces_articles(client, args.pipeline_timeout))

    print("\nread paths")
    runner.check("search", lambda: check_search(client, user_id, subscribed))
    runner.check("search cursor paging", lambda: check_search_paging(client, user_id, subscribed))
    runner.check("feed watermark + ack", lambda: check_feed_watermark(client, user_id))
    runner.check("unsubscribe", lambda: check_unsubscribe(client, user_id, subscribed))

    if args.skip_push:
        print("\npush\n  [skip] digest push check")
    else:
        print("\npush")
        runner.check("digest push fired", lambda: check_digest_push(client, args.push_timeout))

    total = runner.passed + runner.failed
    print(f"\n{'=' * 52}\n{runner.passed}/{total} checks passed, {runner.failed} failed\n")
    return 0 if runner.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
