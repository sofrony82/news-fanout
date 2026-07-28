# news-fanout

News ingestion, topic classification, fan-out-on-read feed and digest push
notifications. An executable version of the design in
[news-fanout-system-design.md](news-fanout-system-design.md).

The load-bearing idea: **an article is written once**, and delivery splits into two
channels with different cost curves — the in-app feed is fan-out-on-read with a
per-user watermark (a million subscribers never materialise), while push is the only
true O(N) path and is made cheap by digesting, filtering inactive users, batching into
multicasts, and pacing through a checkpointed page-job pipeline.

## Run it

```bash
docker compose up --build -d
python3 scripts/e2e_test.py          # 14/14 checks
```

Then browse <http://localhost:8000/docs>.

Deploy to GCP with one command — see **[DEPLOY.md](DEPLOY.md)**:

```bash
cp deploy/config.env.example deploy/config.env   # set PROJECT_ID
./deploy/gcp-deploy.sh
```

## API

| Endpoint | Purpose |
|---|---|
| `GET /v1/topics` | The fixed topic set |
| `POST /v1/subscribe` / `POST /v1/unsubscribe` | Manage subscriptions (`{"topics": [...]}`) |
| `GET /v1/subscriptions` | Current subscriptions |
| `POST /v1/devices` | Register a push token |
| `GET /v1/search?topics=a&topics=b&cursor=&limit=` | Recent articles by topic, newest first |
| `GET /v1/feed` | Unread articles across subscribed topics, plus watermarks |
| `POST /v1/feed/ack` | Advance the read watermark (`{"watermarks": {"sports": 1234}}`) |
| `GET /healthz` / `GET /readyz` | Liveness / readiness (readiness pings Postgres + Redis) |
| `GET /internal/stats` | Pipeline counters — how the test observes ingest and push |
| `GET /metrics` | Prometheus |

Requests carry the caller in an `X-User-Id` header. That is a demo stand-in for
authentication, not a design choice — see [DEPLOY.md](DEPLOY.md) Appendix A.

## Layout

| File | Role |
|---|---|
| [app.py](src/news_fanout/app.py) | Assembles FastAPI, starts the workers for the configured role |
| [api.py](src/news_fanout/api.py) | HTTP surface |
| [workers.py](src/news_fanout/workers.py) | Ingest job, classifier, push coordinator, push workers |
| [repository.py](src/news_fanout/repository.py) | All SQL. Job claiming uses `FOR UPDATE SKIP LOCKED` |
| [adapters.py](src/news_fanout/adapters.py) | Swappable boundaries: article source, classifier, push sender, rate limiter |
| [schemas.py](src/news_fanout/schemas.py) / [models.py](src/news_fanout/models.py) | SQLAlchemy tables / Pydantic payloads |
| [dedup.py](src/news_fanout/dedup.py) | Redis `(user, post_id)` markers so retries never double-push |
| [migrations.py](src/news_fanout/migrations.py) | Applies [schema.sql](src/news_fanout/schema.sql) under an advisory lock |
| [scripts/e2e_test.py](scripts/e2e_test.py) | End-to-end test, stdlib only, runs against any base URL |

### Roles

One image, four roles, selected by `NEWS_FANOUT_ROLE` (`api`, `ingest`, `classifier`,
`push`, or `all`). The compose files use `all`; splitting them into separate services
is how this scales out without code changes.

### The three stubs

`adapters.py` holds the boundaries that a production deployment replaces. Each is a
`Protocol`, so the substitution is a constructor argument in `app.py` — no other code
changes.

- **`StubArticleSource`** — synthesises articles instead of calling provider APIs.
  Bound it with `NEWS_FANOUT_INGEST__STUB_MAX_PAGE_ID` so a long-running demo cannot
  fill its disk.
- **`StubTopicClassifier`** — hashes title+body to pick topics. The real one calls an
  LLM with structured output; the design doc's point that model choice is an offline
  quality exercise against a golden set is the part worth keeping.
- **`LoggingPushSender`** — logs what it would have sent instead of calling APNs/FCM.
  Tokens ending `-dead` are reported invalid, which exercises the token-pruning path.

## Configuration

Pydantic settings, `NEWS_FANOUT_` prefix, `__` for nesting — so
`NEWS_FANOUT_DATABASE__HOST` sets `database.host`. See
[config.py](src/news_fanout/config.py) for every knob and
[.env.example](.env.example) for the ones that matter.

Two worth knowing:

- `NEWS_FANOUT_PUSH__DIGEST_INTERVAL_SECONDS` — the digest window. Lower it to watch
  the push pipeline work; raise it in production, since collapsing
  users×articles into users×windows is what makes push affordable at all.
- `NEWS_FANOUT_SERVER__AUTO_MIGRATE` — applies `schema.sql` on startup. Leave on for a
  single replica; turn off and run `news-fanout migrate` as a separate step once there
  is more than one.

## Notes on this repo

Extracted from a larger uv workspace, where it was `apps/news-fanout` and depended on
an editable `packages/logging-helpers`. That package is not published, so the
dependency is replaced by [logging_setup.py](src/news_fanout/logging_setup.py) —
stdlib-only structured logging, JSON by default and `LOG_FORMAT=text` for local
reading.
