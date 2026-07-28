CREATE SEQUENCE IF NOT EXISTS article_post_id_seq AS bigint;

CREATE TABLE IF NOT EXISTS topics (
    topic_id text PRIMARY KEY,
    name text NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    source_id text PRIMARY KEY,
    page_id_from bigint NOT NULL DEFAULT 0,
    poll_interval_seconds integer NOT NULL DEFAULT 60,
    last_polled_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS articles (
    article_id bigserial PRIMARY KEY,
    source_id text NOT NULL REFERENCES sources (source_id),
    external_id text NOT NULL,
    title text NOT NULL,
    source_url text NOT NULL,
    published_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT articles_source_external_uniq UNIQUE (source_id, external_id)
);

CREATE TABLE IF NOT EXISTS article_bodies (
    article_id bigint PRIMARY KEY REFERENCES articles (article_id) ON DELETE CASCADE,
    body text NOT NULL
);

CREATE TABLE IF NOT EXISTS classify_jobs (
    job_id bigserial PRIMARY KEY,
    article_id bigint NOT NULL UNIQUE REFERENCES articles (article_id) ON DELETE CASCADE,
    status text NOT NULL DEFAULT 'queued',
    attempts integer NOT NULL DEFAULT 0,
    run_after timestamptz NOT NULL DEFAULT now(),
    lease_until timestamptz,
    leased_by text,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS classify_jobs_claim_idx ON classify_jobs (status, run_after);

CREATE TABLE IF NOT EXISTS articles_by_topic (
    topic_id text NOT NULL REFERENCES topics (topic_id),
    post_id bigint NOT NULL DEFAULT nextval('article_post_id_seq'),
    article_id bigint NOT NULL REFERENCES articles (article_id) ON DELETE CASCADE,
    title text NOT NULL,
    source_url text NOT NULL,
    published_at timestamptz NOT NULL,
    confidence double precision NOT NULL,
    PRIMARY KEY (topic_id, post_id),
    CONSTRAINT articles_by_topic_article_uniq UNIQUE (topic_id, article_id)
);

CREATE TABLE IF NOT EXISTS users (
    user_id text PRIMARY KEY,
    device_token text,
    last_active_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS users_last_active_idx ON users (last_active_at);

CREATE TABLE IF NOT EXISTS subscriptions (
    topic_id text NOT NULL REFERENCES topics (topic_id),
    user_id text NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (topic_id, user_id)
);

CREATE INDEX IF NOT EXISTS subscriptions_by_user_idx ON subscriptions (user_id, topic_id);

CREATE TABLE IF NOT EXISTS feed_offsets (
    user_id text NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    topic_id text NOT NULL REFERENCES topics (topic_id),
    last_seen_post_id bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, topic_id)
);

CREATE TABLE IF NOT EXISTS topic_digest_state (
    topic_id text PRIMARY KEY REFERENCES topics (topic_id),
    last_pushed_post_id bigint NOT NULL DEFAULT 0,
    last_pushed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS push_page_jobs (
    job_id bigserial PRIMARY KEY,
    topic_id text NOT NULL REFERENCES topics (topic_id),
    post_id_from bigint NOT NULL,
    post_id_to bigint NOT NULL,
    article_count integer NOT NULL,
    user_id_from text,
    user_id_to text,
    cursor_checkpoint text,
    status text NOT NULL DEFAULT 'queued',
    attempts integer NOT NULL DEFAULT 0,
    run_after timestamptz NOT NULL DEFAULT now(),
    lease_until timestamptz,
    leased_by text,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS push_page_jobs_claim_idx ON push_page_jobs (status, run_after);

INSERT INTO topics (topic_id, name) VALUES
    ('sports', 'Sports'),
    ('politics', 'Politics'),
    ('business', 'Business'),
    ('technology', 'Technology'),
    ('science', 'Science'),
    ('health', 'Health'),
    ('culture', 'Culture'),
    ('world', 'World')
ON CONFLICT (topic_id) DO NOTHING;

INSERT INTO sources (source_id, poll_interval_seconds) VALUES
    ('stub-wire', 5),
    ('stub-daily', 15)
ON CONFLICT (source_id) DO NOTHING;
