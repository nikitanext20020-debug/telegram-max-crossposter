# Automated Telegram-to-MAX Publisher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an owner-controlled Telegram source reader and durable scheduler that publishes unique, link-cleaned posts to the new MAX channel with the mandatory editable `НАШ ТГК` footer.

**Architecture:** A Python 3.12 FastAPI web service exposes a Telegram admin webhook and signed QStash job endpoints. Telethon reads source history and large media, SQLAlchemy persists all queue and dedup state in Neon Postgres, and a focused MAX client streams uploads and publishes HTML messages. Pure domain modules remain independent of external services so queue, sanitation, authorization, and scheduling behavior can be tested against real in-memory components.

**Tech Stack:** Python 3.12, FastAPI, Telethon 1.44, SQLAlchemy 2 async, asyncpg, aiosqlite for tests, HTTPX, Upstash QStash Python SDK, pytest, pytest-asyncio, Ruff.

## Global Constraints

- Only Telegram numeric ID `8235497168` may administer the service.
- Initial source: `https://t.me/animeworldmem`.
- Destination: `https://max.ru/channel_animenaruto`; use its newly discovered numeric `MAX_CHAT_ID`, never the old channel ID by assumption.
- Initial schedule in `Europe/Moscow`: `08:00 any`, `10:00 any`, `12:00 any`, `14:00 video`, `18:00 any`, `21:00 any`.
- Queue target is 12 pending posts and low-water mark is 6.
- Remove every source link/button; preserve ordinary text formatting; append only `<a href="https://t.me/webm4ik">НАШ ТГК</a>` at send time.
- Source post, Telegram media identity, metadata fingerprint, and final SHA-256 duplicates must never be intentionally published.
- `/now` publishes immediately without consuming or reassigning scheduled queue rows.
- Large media is written to temporary disk and streamed; never load a whole media file into RAM.
- Runtime secrets stay in environment variables and never appear in logs, fixtures, commits, or health responses.

---

## File structure

- `app/config.py`: strict environment parsing and non-secret defaults.
- `app/models.py`: SQLAlchemy tables and enums.
- `app/database.py`: async engine/session setup and schema initialization.
- `app/text.py`: Telegram entity normalization, link removal, HTML rendering, footer validation.
- `app/domain.py`: candidate media/value objects and deterministic fingerprints.
- `app/store.py`: durable source, candidate, queue, lease, and dedup operations.
- `app/queueing.py`: slot generation, ranking, round-robin selection, and refill orchestration.
- `app/telegram_reader.py`: Telethon lifecycle, link resolution/join, scans, albums, metrics, downloads.
- `app/max_client.py`: MAX upload slot, streaming multipart upload, readiness retry, send result parsing.
- `app/publisher.py`: claim/download/hash/dedup/upload/send/finalize workflow.
- `app/bot_api.py`: Telegram Bot API responses and owner webhook parsing.
- `app/commands.py`: command grammar and owner-facing command service.
- `app/qstash.py`: signature verification and schedule reconciliation.
- `app/main.py`: FastAPI composition and HTTP endpoints.
- `app/cli.py`: safe session generation, database initialization, webhook/schedule setup, MAX discovery.
- `migrations/001_initial.sql`: equivalent reviewed Postgres schema for Neon console/manual recovery.
- `tests/`: behavior and boundary tests grouped by the production module above.

### Task 1: Python service scaffold and strict configuration

**Files:**
- Create: `pyproject.toml`
- Create: `requirements.txt`
- Create: `requirements-dev.txt`
- Create: `app/__init__.py`
- Create: `app/config.py`
- Create: `tests/test_config.py`

**Interfaces:**
- Produces: `Settings.from_env(env: Mapping[str, str]) -> Settings`, `ScheduleRule(time: time, content_type: ContentType)`.

- [ ] **Step 1: Write failing configuration tests** proving required secrets are named without revealing values, owner defaults to `8235497168`, the six schedule rules parse exactly, and malformed IDs/URLs/time rules fail.

```python
def test_defaults_owner_signature_and_schedule(complete_env):
    settings = Settings.from_env(complete_env)
    assert settings.owner_telegram_id == 8235497168
    assert settings.signature_text == "НАШ ТГК"
    assert settings.signature_url == "https://t.me/webm4ik"
    assert [(x.time.isoformat(timespec="minutes"), x.content_type.value) for x in settings.default_schedule] == [
        ("08:00", "any"), ("10:00", "any"), ("12:00", "any"),
        ("14:00", "video"), ("18:00", "any"), ("21:00", "any"),
    ]
```

- [ ] **Step 2: Run `pytest tests/test_config.py -q` and confirm import failure.**
- [ ] **Step 3: Implement immutable settings with explicit parsing and no environment reads at import time.**
- [ ] **Step 4: Run `pytest tests/test_config.py -q` and `ruff check app tests`.**
- [ ] **Step 5: Commit `feat: scaffold Python service configuration`.**

### Task 2: Link-free formatted text and mandatory footer

**Files:**
- Create: `app/text.py`
- Create: `tests/test_text.py`

**Interfaces:**
- Produces: `clean_telegram_text(text: str, entities: Sequence[TextEntity]) -> CleanText`, `render_max_html(clean: CleanText) -> str`, `build_signature_html(text: str, url: str) -> str`, `append_signature(html: str, signature: str, limit: int = 4000) -> list[str]`.

- [ ] **Step 1: Write failing table-driven tests** with hand-authored expected HTML for bold/italic/underline/code, UTF-16 emoji offsets, visible URLs, hidden links, `@username`, buttons, promotional lines with orphan emoji, textless posts, split text, and the only allowed signature link.

```python
def test_removes_promotional_link_line_without_orphan_emoji():
    clean = clean_telegram_text(
        "Обычный текст\n👀 ХОТ КОНТЕНТ",
        [TextEntity("text_link", 15, 11, "https://t.me/foreign")],
    )
    assert render_max_html(clean) == "Обычный текст"

def test_hidden_link_in_sentence_keeps_plain_words():
    clean = clean_telegram_text(
        "Можно открыть описание здесь.",
        [TextEntity("text_link", 23, 5, "https://example.com")],
    )
    assert render_max_html(clean) == "Можно открыть описание здесь."
```

- [ ] **Step 2: Run `pytest tests/test_text.py -q` and confirm the missing-module failure.**
- [ ] **Step 3: Implement surrogate-aware entity ranges, URL/mention removal, promotional-line cleanup, safe nested formatting, escaping, splitting, and footer validation.**
- [ ] **Step 4: Run text tests, then all tests; mutate link rendering mentally so each unsafe branch is protected.**
- [ ] **Step 5: Commit `feat: sanitize source links and enforce MAX footer`.**

### Task 3: Persistent schema and real in-memory database behavior

**Files:**
- Create: `app/models.py`
- Create: `app/database.py`
- Create: `app/domain.py`
- Create: `app/store.py`
- Create: `migrations/001_initial.sql`
- Create: `tests/test_store.py`

**Interfaces:**
- Produces: `Database.start()`, `Database.session()`, `Store.add_source()`, `Store.upsert_candidate()`, `Store.ensure_default_settings()`, `Store.replace_schedule()`, `Store.claim_due_item()`, `Store.complete_item()`, `Store.fail_item()`, `Store.record_media_hash()`.
- Consumes: settings and content enums from Task 1; cleaned HTML and fingerprints from Task 2.

- [ ] **Step 1: Write failing async tests against a real SQLite in-memory engine** for unique source/message candidates, album identity, disabled-source history, settings persistence, atomic queue lease, expired-lease recovery, unique scheduled times, and SHA-256 duplicate rejection.

```python
async def test_final_hash_can_only_be_published_once(store):
    first = await store.record_media_hash("a" * 64, candidate_id=1)
    second = await store.record_media_hash("a" * 64, candidate_id=2)
    assert first is True
    assert second is False
```

- [ ] **Step 2: Run `pytest tests/test_store.py -q` and confirm model/store imports fail.**
- [ ] **Step 3: Implement focused SQLAlchemy models, typed value objects, async store transactions, and matching Postgres DDL.**
- [ ] **Step 4: Run store tests twice to catch leaked transaction state, then run `ruff check`.**
- [ ] **Step 5: Commit `feat: persist sources queue and duplicate history`.**

### Task 4: Queue planning, ranking, content rules, and refill

**Files:**
- Create: `app/queueing.py`
- Create: `tests/test_queueing.py`

**Interfaces:**
- Produces: `next_slots(now, rules, count, tz)`, `engagement_score(candidate, top_mode=False)`, `QueuePlanner.refill(now, target=12)`, `QueuePlanner.refresh(now)`, `QueuePlanner.replacement(slot)`.
- Consumes: `Store` from Task 3 and `Candidate`/`ScheduleRule` value objects.

- [ ] **Step 1: Write failing tests** for Moscow-time future slots, DST-safe timezone use, `14:00` video-only selection, round-robin sources, top-mode ranking, twelve-row target, low-water refill, refresh preserving published rows, and no substitution when a typed slot is empty.

```python
def test_video_rule_never_selects_image():
    chosen = choose_candidate(
        slot_rule=ContentType.VIDEO,
        candidates=[candidate(1, "image", 100), candidate(2, "video", 1)],
        recent_source_ids=[],
    )
    assert chosen.id == 2
```

- [ ] **Step 2: Run the queue test file and confirm missing behavior.**
- [ ] **Step 3: Implement deterministic slot generation and selection with database uniqueness as the concurrency backstop.**
- [ ] **Step 4: Run queue/store tests and full suite.**
- [ ] **Step 5: Commit `feat: plan and refill typed publication queue`.**

### Task 5: Telethon source reader, join flow, history, and large download

**Files:**
- Create: `app/telegram_reader.py`
- Create: `tests/test_telegram_reader.py`

**Interfaces:**
- Produces: `TelegramReader.verify_session()`, `add_source(link)`, `scan_source(source, window)`, `get_post(link)`, `download_candidate(candidate, directory)`.
- Consumes: `Store.upsert_candidate`, `clean_telegram_text`, and domain media metadata.

- [ ] **Step 1: Write failing adapter-boundary tests** with complete Telethon-shaped message fixtures for public join, already joined, private invite, approval pending, flood wait, date/count boundaries, albums, views/forwards/reactions, photo/video/document metadata, and file streaming to a real temporary file.

```python
async def test_scan_groups_album_and_persists_one_candidate(reader, fake_client):
    fake_client.messages = [album_message(11, 99), album_message(10, 99)]
    result = await reader.scan_source(source(), ScanWindow(limit=100))
    assert result.candidates == 1
    assert result.items[0].message_ids == (10, 11)
```

- [ ] **Step 2: Run the reader tests and verify failure is due to missing adapter.**
- [ ] **Step 3: Implement a short-lived Telethon 1.44 client using `StringSession`, `JoinChannelRequest`/invite import, `iter_messages`, and file-path downloads.**
- [ ] **Step 4: Run reader tests, then full suite; verify no fixture includes live credentials.**
- [ ] **Step 5: Commit `feat: read Telegram sources and large media with Telethon`.**

### Task 6: MAX streaming client and idempotent publisher

**Files:**
- Create: `app/max_client.py`
- Create: `app/publisher.py`
- Create: `tests/test_max_client.py`
- Create: `tests/test_publisher.py`

**Interfaces:**
- Produces: `MaxClient.upload(path, media_type)`, `MaxClient.send(html, attachments)`, `Publisher.publish_due(slot)`, `Publisher.publish_now(link)`.
- Consumes: queue leases/store, reader download, cleaned HTML, current signature, SHA-256 records.

- [ ] **Step 1: Write failing tests** using HTTPX MockTransport and real temporary files for MAX upload slots, streaming multipart bodies, image/photo payloads, video tokens, `attachment.not.ready` delays, 4000-character split, required link-preview disable flag, final-hash duplicate replacement, cleanup in `finally`, and ambiguous send outcomes not retried.

```python
async def test_duplicate_final_hash_is_skipped_before_max_send(publisher, store):
    await store.record_media_hash(FILE_SHA, candidate_id=1)
    outcome = await publisher.publish_candidate(candidate_id=2)
    assert outcome.state == "skipped_duplicate"
    assert publisher.max_client.sent_messages == []
```

- [ ] **Step 2: Run both test files and confirm missing-client failures.**
- [ ] **Step 3: Implement chunked hashing, preflight limits, file-handle upload, bounded readiness retries, MAX payload parsing, atomic completion, replacement, and temp cleanup.**
- [ ] **Step 4: Run publisher/MAX tests and full suite with warnings treated as errors.**
- [ ] **Step 5: Commit `feat: publish deduplicated large media to MAX`.**

### Task 7: Owner commands and Telegram Bot API webhook

**Files:**
- Create: `app/bot_api.py`
- Create: `app/commands.py`
- Create: `tests/test_commands.py`
- Create: `tests/test_bot_api.py`

**Interfaces:**
- Produces: `CommandParser.parse(text)`, `CommandService.execute(command, owner_id)`, `BotApi.send_message(chat_id, text)`, `handle_telegram_update(update)`.
- Consumes: source reader, queue planner, store settings/schedule, immediate publisher.

- [ ] **Step 1: Write failing tests** for every command grammar, bare `t.me` source links, numeric-ID authorization, source add/delete/list, parse windows, queue view, refresh, `/now` queue preservation, schedule replacement, typed slot, signature view/change/reset, invalid input, and long-job lock messages.

```python
async def test_username_cannot_bypass_numeric_owner_check(service):
    result = await service.handle_update(update(from_id=1, username="pythonvoin", text="/status"))
    assert result.ignored == "unauthorized"
```

- [ ] **Step 2: Run command/bot tests and confirm the expected missing behavior.**
- [ ] **Step 3: Implement strict parsing, owner checks before dispatch, Russian responses, background job acknowledgement, and Bot API error handling.**
- [ ] **Step 4: Run command/bot tests and the full suite.**
- [ ] **Step 5: Commit `feat: add owner Telegram control commands`.**

### Task 8: Signed QStash jobs, schedule synchronization, and FastAPI composition

**Files:**
- Create: `app/qstash.py`
- Create: `app/main.py`
- Create: `tests/test_qstash.py`
- Create: `tests/test_main.py`

**Interfaces:**
- Produces: `QStashService.verify(body, signature, url)`, `reconcile(rules)`, FastAPI `app` with `/healthz`, owner webhook, `/jobs/maintain`, and `/jobs/publish`.
- Consumes: all services from Tasks 1-7.

- [ ] **Step 1: Write failing tests** for signature rejection/acceptance, deterministic schedule IDs, `CRON_TZ=Europe/Moscow`, six publish schedules plus one maintenance schedule, removal of stale managed schedules, raw-body verification, Telegram secret header, database health, idempotent repeated job delivery, and correct HTTP retry status.

```python
def test_publish_cron_uses_moscow_timezone():
    assert cron_for_slot(time(14, 0)) == "CRON_TZ=Europe/Moscow 0 14 * * *"
```

- [ ] **Step 2: Run QStash/main tests and confirm missing imports.**
- [ ] **Step 3: Implement official QStash `Receiver`, stable schedule IDs, FastAPI lifespan, raw request verification, dependency composition, and structured secret-free logging.**
- [ ] **Step 4: Run API tests, full tests, Ruff, and a local `/healthz` smoke with a temporary SQLite database.**
- [ ] **Step 5: Commit `feat: expose signed scheduled publishing service`.**

### Task 9: CLI, deployment configuration, and operator documentation

**Files:**
- Create: `app/cli.py`
- Modify: `Dockerfile`
- Modify: `render.yaml`
- Modify: `.env.example`
- Modify: `README.md`
- Delete: obsolete Node runtime files only after Python parity is green: `package.json`, `src/*.mjs`, `scripts/*.mjs`, `test/*.mjs`
- Create: `tests/test_cli.py`

**Interfaces:**
- Produces commands `python -m app.cli init-db`, `session`, `verify-session`, `discover-max`, `register-webhook`, `sync-schedules`, and `smoke`.

- [ ] **Step 1: Write failing CLI tests** proving environment secrets are consumed but never echoed, MAX discovery prints only candidate chat IDs/titles, webhook registration targets the configured owner bot, and smoke checks database/Telegram/MAX without publishing unless `--publish` is explicit.
- [ ] **Step 2: Run CLI tests and confirm missing command failures.**
- [ ] **Step 3: Implement CLI commands, Python Docker image as non-root, Render Free Web Service config, complete secret placeholders, migration/runbook, command reference, and credential-rotation warning.**
- [ ] **Step 4: Delete Node runtime only after `pytest -q`, `ruff check .`, `ruff format --check .`, and Docker build all pass.**
- [ ] **Step 5: Commit `feat: deploy automated publisher on Render`.**

### Task 10: Production setup and delivery verification

**Files:**
- Modify only if verification exposes a defect: files introduced above.

**Interfaces:**
- Consumes CLI/runbook and deployed service; produces concrete deployment evidence.

- [ ] **Step 1: Push `codex/automation-queue` and let Render deploy the exact pushed commit.**
- [ ] **Step 2: Create/configure Neon and QStash Free resources; enter secrets directly in provider dashboards without copying them into logs or chat.**
- [ ] **Step 3: Generate or verify the Telethon StringSession locally, discover the numeric ID for `channel_animenaruto`, and update Render environment variables.**
- [ ] **Step 4: Initialize schema, register admin webhook, synchronize schedules, add `animeworldmem`, and run `/parse` to fill twelve unique rows.**
- [ ] **Step 5: Verify production `/healthz`, `/status`, unauthorized rejection, queue persistence after restart/sleep, a >20 MB video upload, link cleanup, exact `НАШ ТГК` footer, and one scheduled delivery at the expected Moscow time.**
- [ ] **Step 6: Record deployed commit, service URL, test counts, real MAX message evidence, and any provider limitations in the handoff without exposing secrets.**

## Self-review

- Every acceptance requirement in the approved design maps to Tasks 2-10.
- No implementation step depends on an undefined interface; each consumer names the producing task.
- External network behavior is tested at our HTTP/request boundaries; domain behavior uses real components and a real in-memory database.
- The plan contains no deferred placeholders, automatic ambiguous-send retry, secret export, or dependency on Render's ephemeral filesystem.
