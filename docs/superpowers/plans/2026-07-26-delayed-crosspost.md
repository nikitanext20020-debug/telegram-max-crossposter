# Delayed Crosspost Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wait 10 seconds for Telegram cleanup edits and publish only the final version of a channel post to MAX.

**Architecture:** Extend the webhook update router to accept both new and edited channel posts. Keep a small in-memory pending queue keyed by source chat and message ID; each update replaces the pending message and restarts a 10-second timer. When the timer expires, the existing `crosspostMessages` function receives the latest message unchanged. Album collection remains separate and receives the same settling delay after its current 1.5-second grouping window.

**Tech Stack:** Node.js 22, native `node:http`, native timers, Node test runner, existing Telegram/MAX modules.

## Global Constraints

- The settling delay is exactly 10 seconds.
- Existing duplicate-update protection must remain active.
- Existing MAX publishing, media handling, formatting, and signature behavior must remain unchanged.
- Full Telegram post deletion synchronization is out of scope.
- Pending state is in memory; webhook retries remain the recovery mechanism after restarts.

---

### Task 1: Add failing tests for delayed and edited updates

**Files:**
- Modify: `test/server.test.mjs` (or the existing server/webhook test file if the repository names it differently)
- Test: `test/server.test.mjs`

**Interfaces:**
- Consume the server's injected/fake crosspost dependency or the existing test harness.
- Produce assertions that the server does not publish immediately, replaces a pending message on `edited_channel_post`, and publishes the edited text after the timer.

- [ ] **Step 1: Inspect the existing server test harness**

Run:

```bash
rg -n "createServer|telegram_post_received|processUpdate|crosspostMessages|setWebhook" test src scripts
```

Use the existing test seam; do not introduce a second HTTP test framework.

- [ ] **Step 2: Write the failing test for the 10-second settling window**

Use fake timers or the repository's existing timer helper. Send a `channel_post` containing `МЫ В МАКСЕ`, assert that the crosspost spy has zero calls before 10 seconds, then advance the clock to 10 seconds and assert one call.

- [ ] **Step 3: Write the failing test for an edit replacing the pending message**

Send the original `channel_post`, advance the clock by 9 seconds, send an `edited_channel_post` with the cleaned text, advance one second, assert zero publishes, advance the remaining 9 seconds, and assert the single published message contains the cleaned text and not `МЫ В МАКСЕ`.

- [ ] **Step 4: Write the failing test for independent messages**

Send two different message IDs from the same channel, edit only the first, advance timers, and assert each message publishes once with its own final text.

- [ ] **Step 5: Run only the new tests and verify the expected failure**

Run:

```bash
npm test -- --test-name-pattern="settling|edited|independent"
```

Expected: the tests fail because the current server publishes immediately and ignores `edited_channel_post`.

- [ ] **Step 6: Commit the failing tests**

```bash
git add test/server.test.mjs
git commit -m "test: cover delayed crosspost cleanup edits"
```

### Task 2: Implement the pending-message settling queue

**Files:**
- Modify: `src/server.mjs`
- Modify: `scripts/register-telegram-webhook.mjs`
- Modify: `src/config.mjs` only if the delay needs a named constant; keep the default fixed at 10 seconds unless an existing configuration pattern requires otherwise.

**Interfaces:**
- `processUpdate(update)` must accept `channel_post` and `edited_channel_post`.
- The pending queue key is `${message.chat.id}:${message.message_id}`.
- Each queue entry stores the latest Telegram message and its timer.
- The timer callback invokes existing `crosspostMessages(config, [latestMessage])`.

- [ ] **Step 1: Add the minimal update extraction helper**

Implement a helper that returns `{ message, edited }` from either `update.channel_post` or `update.edited_channel_post`, with edited updates taking the same source-chat validation path.

- [ ] **Step 2: Add the 10-second pending queue**

On every new or edited message, clear the prior timer for its key, store the latest message, and schedule the publish callback for 10,000 milliseconds. Only the callback that owns the current entry may publish and delete that entry.

- [ ] **Step 3: Preserve album behavior**

Keep the existing 1.5-second media-group batching, but route the resulting batch through the same 10-second settling delay so cleanup edits can arrive before MAX publication.

- [ ] **Step 4: Subscribe Telegram to edited channel posts**

Change `allowed_updates` from:

```js
["channel_post"]
```

to:

```js
["channel_post", "edited_channel_post"]
```

- [ ] **Step 5: Run the focused tests and verify they pass**

Run:

```bash
npm test -- --test-name-pattern="settling|edited|independent"
```

Expected: all new tests pass.

- [ ] **Step 6: Commit the implementation**

```bash
git add src/server.mjs scripts/register-telegram-webhook.mjs
git commit -m "feat: wait for Telegram cleanup before crossposting"
```

### Task 3: Verify, register the updated webhook, and deploy

**Files:**
- Modify: `README.md` only if the operational delay or webhook update command is not documented.

**Interfaces:**
- The deployed service continues to expose `GET /healthz` and `POST /telegram`.
- Telegram webhook reports `allowed_updates` containing both channel-post types.

- [ ] **Step 1: Run the complete local verification suite**

Run:

```bash
npm test
npm run typecheck
```

Expected: every test passes and the type check exits successfully.

- [ ] **Step 2: Push the implementation commit**

```bash
git push origin main
```

- [ ] **Step 3: Wait for Render to deploy the pushed commit**

Confirm the Render deploy reaches Live and the service logs show it is listening on the configured port.

- [ ] **Step 4: Re-register the Telegram webhook**

Run the existing registration script against the deployed `PUBLIC_URL`; verify the response is successful and `getWebhookInfo` reports both `channel_post` and `edited_channel_post`.

- [ ] **Step 5: Verify the deployed health endpoint**

Run:

```bash
curl -fsS https://telegram-max-crossposter.onrender.com/healthz
```

Expected:

```json
{"ok":true}
```

- [ ] **Step 6: Perform the end-to-end cleanup test**

Publish a test post through the Telegram userbot that initially contains the marker, let the userbot remove it within 10 seconds, and confirm MAX receives only the cleaned text with the configured signature.
