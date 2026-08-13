CREATE TABLE IF NOT EXISTS settings (
    id integer PRIMARY KEY CHECK (id = 1),
    owner_telegram_id bigint NOT NULL,
    timezone varchar(64) NOT NULL,
    signature_text varchar(500) NOT NULL,
    signature_url varchar(2000) NOT NULL,
    max_chat_id varchar(128) NOT NULL,
    queue_target integer NOT NULL,
    queue_low_water integer NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id bigserial PRIMARY KEY,
    link varchar(2000) UNIQUE NOT NULL,
    telegram_chat_id bigint UNIQUE,
    title varchar(500) NOT NULL DEFAULT '',
    enabled boolean NOT NULL DEFAULT true,
    join_state varchar(32) NOT NULL DEFAULT 'new',
    cursor_message_id bigint,
    last_scanned_at timestamptz,
    retry_after timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS schedule_slots (
    id bigserial PRIMARY KEY,
    local_time varchar(5) UNIQUE NOT NULL,
    content_type varchar(16) NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    qstash_schedule_id varchar(200)
);

CREATE TABLE IF NOT EXISTS candidates (
    id bigserial PRIMARY KEY,
    source_id bigint NOT NULL REFERENCES sources(id),
    telegram_chat_id bigint NOT NULL,
    source_post_key varchar(200) NOT NULL,
    message_ids jsonb NOT NULL,
    album_group_id varchar(200),
    published_at timestamptz NOT NULL,
    cleaned_html text NOT NULL,
    media_kind varchar(16) NOT NULL,
    media_json jsonb NOT NULL,
    telegram_media_identity varchar(64) UNIQUE,
    metadata_fingerprint varchar(64) UNIQUE NOT NULL,
    views bigint NOT NULL DEFAULT 0,
    forwards bigint NOT NULL DEFAULT 0,
    reactions bigint NOT NULL DEFAULT 0,
    state varchar(32) NOT NULL DEFAULT 'eligible',
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_candidate_source_post UNIQUE (source_id, source_post_key)
);

CREATE TABLE IF NOT EXISTS queue_items (
    id bigserial PRIMARY KEY,
    candidate_id bigint UNIQUE NOT NULL REFERENCES candidates(id),
    scheduled_at timestamptz UNIQUE NOT NULL,
    slot_rule varchar(16) NOT NULL,
    state varchar(32) NOT NULL DEFAULT 'pending',
    lease_token varchar(64),
    lease_until timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    last_error text,
    max_message_ids jsonb,
    published_at timestamptz
);

CREATE TABLE IF NOT EXISTS media_hashes (
    sha256 varchar(64) PRIMARY KEY,
    candidate_id bigint NOT NULL REFERENCES candidates(id),
    max_message_ids jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS publish_attempts (
    id bigserial PRIMARY KEY,
    queue_item_id bigint REFERENCES queue_items(id),
    candidate_id bigint NOT NULL REFERENCES candidates(id),
    outcome varchar(32) NOT NULL,
    error_code varchar(128),
    detail text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version varchar(100) PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
