# News Ingestion & Topic Notification System — Interview Solution

A step-by-step design for: *ingest articles → classify by topic → notify subscribers → let users search history.* Written to be drawn and narrated on a whiteboard in ~45 minutes.

---

## Step 0 — Clarify before drawing (2–3 min)

State these as questions, then lock the answers:

- **Ingestion**: We have agreements with providers; we pull via their APIs (not scraping). Each provider gives a URL to fetch the article body.
- **Topics**: Fixed, bounded set (~100). Each article maps to one (or a few) existing topics. No dynamic topic creation.
- **Search**: Users pick topics (not free-text keyword search) and get recent articles for those topics, newest first.
- **Notifications**: Push to mobile (APNs / FCM) when new articles land in a subscribed topic.
- **Idempotency on search**: Not needed — it's a read.

> One-liner to set the frame: *"I'll define the API surface first, because it pins down the production requirements, then build the components behind it."*

---

## Step 1 — Scale estimation (do this; it drives every later decision)

Given: **1M articles/day**, **1M users**.

- Articles: 1M/day ≈ **~12 articles/sec** average. Trivial ingest rate. Spiky (World Cup), so size for ~10x burst = ~120/sec.
- Subscriptions: 1M users × ~5 topics each = **~5M subscription edges**.
- **Notification volume (the scary number):** if we pushed one notification per article per subscriber:
  - A hot topic (e.g. "sports") might have ~1M subscribers and dozens of articles/day.
  - Across all users and topics this lands in the **hundreds-of-millions to billions of sends/day** range.

**Conclusion the number forces:** real-time per-article push is economically impossible. **Digest-by-default**, with real-time reserved for a few high-signal topics. This is a *derived* decision, not a "product preference."

---

## Step 2 — API design

```
GET  /v1/search?topics=[t1,t2,...]&cursor=...      → recent articles for topics, newest first
POST /v1/subscribe        body: { topics: [...] }
POST /v1/unsubscribe      body: { topics: [...] }
```

Internal (provider-facing), used by the ingest job:

```
GET /v1/get_articles?page_id_from=<cursor>
    → { page_id_from, page_id_to, articles: [ { id, source_url, ... } ] }
```

Notes:
- Search is `GET`, no idempotency key (pure read).
- Provider pagination uses an **int64 monotonic cursor (`page_id`)**, not a wall-clock timestamp — timestamps drift (clock skew, DST) and aren't strictly monotonic. Store the last-seen `page_id` per source.
- Article bodies can be large → the provider response carries a **reference (URL / object-store key)**, not the full body. We fetch/store the body separately so the control plane stays light.

---

## Step 3 — High-level architecture

```
                        ┌─────────────────┐
   Provider APIs  ◄──────┤  Ingest job     │  (scheduled, per-source cursor)
                        └────────┬────────┘
                                 │ enqueue {article_ref, source, page_id}
                                 ▼
                          ┌────────────┐
                          │   Queue    │  (Kafka / SQS)
                          └─────┬──────┘
                                │
                      ┌─────────▼─────────┐
                      │ Classifier workers│  → LLM (JSON structured output)
                      └─────────┬─────────┘
                                │ write article + topic
                                ▼
                       ┌──────────────────┐
                       │  Cassandra        │
                       │  articles_by_topic│
                       │  subscriptions_*  │
                       │  feed_offsets     │
                       └────┬─────────┬────┘
                            │         │
                ┌───────────▼──┐   ┌──▼──────────────┐
   /v1/search ──┤ Feed (pull)  │   │ Push pipeline   ├──► APNs / FCM
   app open  ───┤ fan-out-on-  │   │ (coordinator +  │
                │ read         │   │  page workers)  │
                └──────────────┘   └─────────────────┘
```

---

## Step 4 — Ingestion (background job)

- Scheduled per source (config-driven interval, e.g. every 1–10 min).
- Calls provider `get_articles?page_id_from=<last_seen>`, advances and persists the cursor.
- For each article: resolve the body (fetch from provider URL, store in object storage), then **enqueue lightweight metadata** `{article_id, source, body_ref}` to the queue.
- **Why a queue:** decouples bursty ingest from classification, lets us scale classifier workers independently, gives at-least-once delivery + retries.

**Config** (separate store): source list, poll intervals, topic list, which LLM/endpoint to use.

---

## Step 5 — Classification

- Classifier workers pull from the queue, fetch the body via `body_ref`, call an **LLM** with **JSON-structured output** → `{ topic_id, confidence }`.
- **Model selection is an offline quality exercise, not a guess:** build a golden labeled dataset, run candidate models (start small, e.g. 8B), compute a **confusion matrix** (false-positive / false-negative per topic), pick the smallest model that meets the quality bar. Re-run when topics or sources change.
- On classify, **write the article into `articles_by_topic`** (Step 6), then signal the notification path (Step 8).

> This is the segment to lean into — it's the ML-infra depth most candidates can't give.

---

## Step 6 — Data model (Cassandra — query-driven, denormalized)

Cassandra is chosen for append-heavy writes, automatic consistent-hash sharding, and horizontal scale. **The rule: one table per access pattern.** Model the *queries*, not the entities.

```
articles_by_topic
  PARTITION KEY  topic_id
  CLUSTERING     post_id DESC        -- monotonic int64
  COLUMNS        article_id, title, body_ref, ts
  -- serves BOTH /v1/search AND the in-app feed (newest-first by topic)

subscriptions_by_topic              -- the inverse index, the piece people forget
  PARTITION KEY  topic_id
  CLUSTERING     user_id
  -- "who subscribes to topic T?" → the push fan-out target

feed_offsets                        -- per-user read watermark (pull path only)
  PARTITION KEY  user_id
  CLUSTERING     topic_id
  COLUMNS        last_seen_post_id

users_by_id
  PARTITION KEY  user_id
  COLUMNS        profile, device_tokens[], active_last_seen, subscribed_topics[]
```

**Common trap:** keying only `user_id → [topics]`. Then "find subscribers of a hot topic" forces a full-table scan. `subscriptions_by_topic` is the table that makes fan-out cheap.

---

## Step 7 — Write path on a new article

A new classified article = **one write**:

```
INSERT into articles_by_topic (topic_id, post_id=<next monotonic>, article_id, body_ref, ts)
```

That's it. We do **not** write a row per subscriber. The million-way fan-out is avoided at write time entirely. Delivery is handled by two separate read/push channels below.

---

## Step 8 — The fan-out problem (the core of the interview)

> **Framing line to open with:**
> *"I never fan out to a million users. I write the article once and split delivery into two channels with different cost curves. The feed is fan-out-on-read, so the million never materializes. Push is the only true O(N), and I make it cheap."*

### Channel A — In-app feed: **fan-out-on-read** (the million never appears)

- No proactive work per subscriber.
- On app open / `/v1/search`, compute unread lazily:

```
for each subscribed topic_id:
    read articles_by_topic WHERE topic_id = T AND post_id > feed_offsets[user, T]
advance feed_offsets[user, T] = max(post_id) when the user views them
```

- This is the **watermark on the read path** (not a periodic scan over all users — that distinction is everything).
- Cost = 1 write per article + work only for users who actually show up. A topic with 1M subscribers but 5% active in the window costs ~50k reads, not 1M.
- This is the Twitter/Instagram "celebrity" pattern: merge at read time instead of pushing into millions of timelines.

### Channel B — Push notifications: **irreducible O(N), made cheap three ways**

Pushing to a device is one external API call per token — there is no "lazy" version of pinging a phone. So we attack **N** directly:

**(1) Shrink N before sending**
- **Digest, don't per-article:** batch a window into one push ("12 new sports stories this hour"). Collapses users×articles → users×windows. During a spike that's a 50–100x cut. *This is what the Step-1 estimate justified.*
- **Active-user filter:** skip users dormant >30 days — they'll get it from feed (Channel A) when they return. Often >50% reduction.
- **Token hygiene:** prune dead/unregistered tokens continuously so you never spend sends on phones that can't receive.

**(2) Batch the sends**
- FCM **multicast packs up to 500 tokens per call** → a true 1M send becomes **~2,000 API calls**, not 1M.
- APNs is per-device but over **HTTP/2 multiplexing** you pipeline thousands of concurrent streams per connection.

**(3) Pace + checkpoint (the pipeline)**

```
Coordinator:
    reads subscriptions_by_topic WHERE topic_id = T in pages of ~10k
    enqueues ~100 page-jobs:  { topic_id, post_id, cursor_range }
            ↓
Push workers (scale horizontally):
    pull a page-job
    expand cursor_range → device tokens (join users_by_id)
    apply active-user filter
    send via FCM multicast / APNs under a token-bucket rate cap (provider quota)
    CHECKPOINT the page cursor on success
    on failure → retry queue (exponential backoff); invalid token → prune async
    dedup on (user_id, post_id) via TTL'd Redis marker (so retries don't double-push)
```

- **Checkpoint is non-negotiable:** if a worker dies at 900k/1M, resume from the last completed page — not from zero.
- The "1M" is now: ~100 page-jobs → ~2,000 multicast calls → paced under quota, resumable, deduped.

### Channel selection (the hybrid = the actual "ideal")

| Topic size       | Feed                | Push                              |
|------------------|---------------------|-----------------------------------|
| Cold (few subs)  | fan-out-on-read     | eager direct send (cheap)         |
| Hot (≫ subs)     | fan-out-on-read     | paced checkpointed page pipeline  |

Branch on subscriber count. Don't pay pipeline overhead for a 50-subscriber topic.

---

## Step 9 — Failure handling & edge cases

- **Queue delivery:** at-least-once → classifier and push must be idempotent (dedup keys).
- **Push dedup:** `(user_id, post_id)` TTL marker prevents double-push on retry.
- **Worker death:** page-cursor checkpoints make push resumable; classifier re-processes safely (idempotent write).
- **Provider rate limits:** token bucket per provider; backoff + dead-letter queue.
- **Hot partition risk:** a single mega-topic partition in `articles_by_topic` can grow unbounded → bucket the partition key by time window (`topic_id:yyyymmdd`) if needed.
- **Invalid tokens:** async prune on APNs/FCM rejection feedback.

---

## Step 10 — 60-second closing summary (always land the plane)

> *"Architecture: ingest job pulls via a monotonic cursor and enqueues metadata; classifier workers tag topic via an LLM chosen against a golden set; one append-only write per article into a query-driven Cassandra schema. Delivery splits in two: the in-app feed is fan-out-on-read with a per-user watermark, so a million subscribers never materialize; push is the only true O(N), and I shrink N with digests and active-user filtering, batch with FCM multicast, and pace with a checkpointed page-job pipeline. The one real risk is hot-topic push throughput under a spike — handled by digesting and pacing. The estimate (billions of potential sends/day) is what makes digest the default."*

---

## The single highest-leverage thing to rehearse

The exact sequence, said in order, that closes the interviewer's repeated objection:

1. **"I write the article once"** — one insert, no per-subscriber rows.
2. **"Feed is fan-out-on-read"** — watermark on read, the million never appears.
3. **"Push is the only true O(N)"** — and here's how I make it cheap: shrink N (digest + active filter) → batch (multicast) → pace + checkpoint (page-job pipeline).
4. **"Here are the four tables"** — `articles_by_topic`, `subscriptions_by_topic`, `feed_offsets`, `users_by_id`.

Separating push from pull is the move that flips this from a borderline read to a clear pass.
