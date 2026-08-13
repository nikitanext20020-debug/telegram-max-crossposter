# Automated Telegram-to-MAX publisher

## Goal

Replace the current Telegram Bot API webhook crossposter with an automated,
persistent publisher that reads configurable Telegram channels through the
owner's Telegram account, keeps a deduplicated queue populated, and publishes
to the new MAX channel on an editable Moscow-time schedule.

The initial source is `https://t.me/animeworldmem`. The destination is the MAX
channel `https://max.ru/channel_animenaruto`. Only Telegram user ID
`8235497168` may administer the service. The username `@pythonvoin` is shown in
documentation only and is never used for authorization.

## Chosen architecture

The existing repository is upgraded to a Python 3.12 service with four focused
adapters around a small application core:

1. A Telethon MTProto client reads public or private Telegram channels using a
   personal-account StringSession. This bypasses the Bot API 20 MB download
   limit and can read source history.
2. A separate Telegram Bot API webhook provides the owner-only control
   interface. It never reads source media.
3. A MAX API client uploads media and publishes HTML messages to the configured
   numeric `MAX_CHAT_ID`.
4. Neon Postgres stores sources, settings, candidates, queue rows, attempts,
   and durable duplicate keys. No durable state is kept on Render's ephemeral
   filesystem.

Render remains a Free Web Service. Upstash QStash calls authenticated HTTP
endpoints to wake it for maintenance and scheduled publication. One maintenance
schedule runs every 30 minutes and one schedule is maintained for each enabled
publication time. With the six defaults this uses seven of QStash Free's ten
active schedule slots.

## Source management

The owner can add a channel by sending a bare `t.me` link or by using
`/add_source <link>`. Public usernames and valid private invite links are
accepted.

On addition, the Telethon account resolves the link and joins the channel when
it is not already a member. If Telegram requires administrator approval, the
source is stored as `pending_join` and the owner is notified. Invalid,
expired, inaccessible, or rate-limited joins are reported without creating an
active source. The service does not automatically leave channels.

`/sources` lists source number, title, link, enabled state, join state, and last
scan time. `/del_source <number|link>` disables the source without deleting its
published history or duplicate keys.

## Candidate collection and ranking

Each scan reads messages newer than the source cursor. Manual commands can also
read older history:

- `/parse` scans recent messages and fills the queue to its target.
- `/parse <count>` examines up to `count` posts per enabled source.
- `/parse_period <Nd|Nh> [count]` scans the requested rolling period.
- `/parse_from <DD.MM.YYYY> [count]` scans from a Moscow calendar date.
- `/parse_top <Nd> [count]` scans the period and prioritizes the most active
  posts.
- `/build` is an alias for `/parse` for compatibility with the existing usage.

A Telegram album is one candidate containing all message IDs in the album.
Views, forwards, and the sum of reactions are stored when Telegram exposes
them. Normal selection balances freshness, engagement, and round-robin source
rotation. `/parse_top` gives engagement the strongest weight. Missing metrics
are treated as zero and never cause a scan to fail.

The scanner runs after service startup, every QStash maintenance wake-up, after
source or schedule changes, after a successful publication, and whenever fewer
than six pending rows remain. It fills twelve future slots by default (two days
at the default six posts per day). Those values are configuration constants,
not owner-facing settings in the first release.

## Schedule and content types

The default timezone is `Europe/Moscow`; all owner-facing dates and times use
that timezone. The initial enabled times are:

- `08:00 any`
- `10:00 any`
- `12:00 any`
- `14:00 video`
- `18:00 any`
- `21:00 any`

`/times` shows the schedule. `/times 08:00,10:00,12:00,14:00,18:00,21:00`
replaces the time list while preserving an existing slot's content rule and
using `any` for new slots. `/slot 14:00 video` and `/slot 14:00 any` change a
single rule. Valid rules in the first release are `any`, `video`, and `image`.

When no candidate matches a typed slot, the service leaves the slot pending,
runs one fresh scan, and tries once more. If it is still empty, it notifies the
owner and does not substitute the wrong media type.

`/queue [count]` shows scheduled time, source, source post ID, type, and state.
`/refresh` removes only unpublished queue assignments and rebuilds them; it
never removes published duplicate history. `/now <Telegram post link>`
publishes the referenced post immediately and leaves scheduled queue rows
untouched. The immediate post still participates in permanent deduplication.

## Durable deduplication

The service must never intentionally publish the same source post twice. It
uses progressively stronger keys:

1. source Telegram chat ID plus message ID or album group ID;
2. Telegram media identity (photo/document IDs) when available;
3. a metadata fingerprint from normalized cleaned caption, media kind, size,
   duration, and dimensions;
4. SHA-256 of each downloaded file before MAX publication.

Database unique constraints make concurrent QStash retries idempotent. A queue
row is claimed atomically. If the final SHA-256 reveals a duplicate, the row is
marked `skipped_duplicate`, a replacement is selected for that same slot, and
the duplicate is not published.

The source cursor advances only after candidates from the scanned range are
persisted. Publication success is stored with returned MAX message IDs. A
failed or ambiguous MAX submission is never blindly repeated: the attempt and
error remain visible for owner review.

## Text, formatting, links, and signature

Supported Telegram formatting is preserved as MAX HTML: bold, italic,
underline, strikethrough, inline code, preformatted text, and block quotes.
All source hyperlinks are removed before the message is queued:

- visible `http://` and `https://` URLs, including `t.me` and `max.ru`;
- hidden Telegram text links;
- Telegram `@username` links;
- Bot API/Telegram buttons and their URLs;
- promotional footer lines containing a removed link, including surrounding
  punctuation and emoji.

For a hidden link inside ordinary prose, the visible words remain as plain
text. For a visible URL, the URL characters are removed. Lines recognized as
link-only or promotional footers are removed as a whole so orphan emoji are not
left behind. Hashtags and unlinked ordinary text remain. Links burned into an
image or video are outside the scope of text filtering.

Immediately before every MAX send, the service appends exactly one configured
linked footer. The initial value is:

```html
<a href="https://t.me/webm4ik">НАШ ТГК</a>
```

This is the only active link allowed in the outgoing MAX HTML. It replaces the
old `ФУЛЛ`/VPN footer. The footer is present even when the source caption is
empty. An album receives one footer on its MAX message. If MAX limits force one
Telegram candidate into multiple MAX messages, every resulting message gets
the footer.

`/signature` shows the current text and URL.
`/signature НАШ ТГК | https://t.me/webm4ik` atomically changes both after
validating an `http` or `https` URL. `/signature_reset` restores the initial
value. There is no disable command because the footer is mandatory. The footer
is read at publication time, so a change also affects already queued posts.

MAX link previews are disabled to prevent the required footer from generating
an unwanted preview card.

## Media handling

Telethon downloads each selected candidate to a unique temporary directory on
Render. The process never loads an entire large file into RAM. It calculates
SHA-256 from fixed-size chunks, opens the file as a streaming multipart upload
to MAX, waits for `attachment.not.ready` using bounded exponential backoff, and
removes the temporary directory in `finally` on success or failure.

The first release enforces MAX's relevant service limits before download:

- image: 50 MB;
- video: 250 MB;
- audio: 256 MB and 60 minutes;
- generic file: 4 GB, additionally bounded by available Render temporary disk.

Oversized or unsupported media is marked failed with a clear owner
notification and replaced in the scheduled slot. Albums retain MAX's maximum
of twelve visual attachments per message; extra items are split into additional
MAX messages with the mandatory footer on each.

## Owner bot commands

The Bot API webhook accepts commands only when `from.id == 8235497168`.
Unauthorized requests receive no operational data. Supported commands are:

```text
/start, /help, /status
/add_source <link>, or a bare Telegram channel link
/sources
/del_source <number|link>
/parse [count]
/parse_period <Nd|Nh> [count]
/parse_from <DD.MM.YYYY> [count]
/parse_top <Nd> [count]
/build
/queue [count]
/refresh
/now <Telegram post link>
/times [comma-separated HH:MM values]
/slot <HH:MM> <any|video|image>
/signature
/signature <text> | <http(s) URL>
/signature_reset
```

Long-running commands acknowledge immediately, acquire a database job lock,
and send a completion summary later. A second conflicting command reports that
the current job is still running rather than starting duplicate work.

## HTTP endpoints and QStash

- `GET /healthz` reports process and database reachability without exposing
  secrets.
- `POST /telegram/webhook/<secret>` accepts owner-bot updates.
- `POST /jobs/maintain` scans sources and fills the queue.
- `POST /jobs/publish?slot=HH:MM` atomically claims and publishes one due row.

The Telegram path contains a high-entropy secret and also validates Telegram's
secret-token header. Job endpoints verify Upstash's QStash signature and reject
unsigned callers. Each job carries an idempotency key; database locks remain
the final concurrency guard.

## Persistence model

Neon migrations create these logical records:

- `settings`: singleton owner, timezone, signature, queue thresholds, and MAX
  destination metadata;
- `sources`: Telegram peer identity, join state, scan cursor, enabled state;
- `schedule_slots`: local time, content rule, enabled state, QStash schedule ID;
- `candidates`: source post/album identity, cleaned HTML, media metadata,
  engagement, fingerprints, and eligibility state;
- `queue_items`: candidate, scheduled UTC timestamp, slot rule, state, lease,
  attempts, and error;
- `media_hashes`: SHA-256 plus published candidate and MAX IDs;
- `publish_attempts`: append-only request outcome audit without secret payloads;
- `schema_migrations`: applied migration versions.

Dates are stored in UTC and rendered in `Europe/Moscow`. Foreign keys and
unique constraints preserve history when a source is disabled.

## Failure behavior

- Render restart: the next QStash call resumes from Postgres; no queue state is
  lost.
- Telegram flood wait: persist the retry-after time, skip that source for the
  current run, and notify the owner once.
- Deleted/inaccessible Telegram post: mark it unavailable and fill the slot
  from another candidate.
- MAX `attachment.not.ready`: bounded exponential retry before message send.
- MAX response before a clearly confirmed message ID: record `ambiguous` and
  do not auto-repeat.
- Database unavailable: return a retryable HTTP status before claiming work.
- QStash duplicate delivery: unique job/queue claims prevent double publish.
- Temporary-file cleanup: always run in `finally`; a startup sweep deletes
  stale task directories left by an interrupted process.

The owner receives concise Telegram notices for failed joins, exhausted
publication slots, flood waits, unsupported/oversized files, ambiguous MAX
outcomes, and repeated infrastructure failures. Health responses and logs must
never contain tokens, phone numbers, StringSessions, API hashes, or full private
post text.

## Configuration and secret handling

Runtime secrets are environment variables only:

```text
TELEGRAM_API_ID
TELEGRAM_API_HASH
TELEGRAM_SESSION_STRING
TELEGRAM_ADMIN_BOT_TOKEN
TELEGRAM_WEBHOOK_SECRET
OWNER_TELEGRAM_ID=8235497168
DATABASE_URL
MAX_BOT_TOKEN
MAX_CHAT_ID
QSTASH_TOKEN
QSTASH_CURRENT_SIGNING_KEY
QSTASH_NEXT_SIGNING_KEY
PUBLIC_URL
```

`MAX_CHAT_ID` must be rediscovered for `channel_animenaruto`; the old numeric
channel ID must not be reused merely because it exists in Render. A local
session-generation command prompts interactively and prints a StringSession for
manual insertion into Render. Credentials and login codes are never committed,
logged, or printed by tests.

## Deployment and migration

The deployment replaces the old immediate webhook behavior after these gates:

1. all unit and integration tests pass locally;
2. a Neon database and migrations are configured;
3. the Telethon StringSession is verified with a read-only `get_me` call;
4. the new MAX numeric chat ID is discovered and a test publication succeeds;
5. QStash schedules are registered from the stored schedule;
6. the Telegram admin webhook is registered and `/status` responds only to the
   owner;
7. Render `/healthz`, maintenance, queue fill, and one real scheduled delivery
   are verified from production logs and destination content.

The old service remains recoverable through Git history. No old Telegram anime
scheduler repository or channel is modified by this project.

## Acceptance tests

- Add `animeworldmem`, join if necessary, scan old and new posts, and persist
  candidates after a Render restart.
- Fill twelve unique future slots and refill after one publication.
- Never queue or publish a source post, media identity, metadata fingerprint,
  or final file hash twice.
- Publish a video larger than 20 MB without loading it wholly into memory.
- Publish the `14:00` slot only with video and respect changed times after
  QStash schedule synchronization.
- Remove visible, hidden, Telegram, MAX, username, and button links while
  preserving ordinary formatting and leaving no promotional emoji-only footer.
- Append only the active `НАШ ТГК` link, including to textless and split posts,
  and apply a changed signature to already queued posts.
- Reject every administrative command from IDs other than `8235497168`.
- Handle QStash retries and concurrent workers without duplicate MAX messages.
- Restore queue processing after a simulated process restart using only
  Postgres state.
