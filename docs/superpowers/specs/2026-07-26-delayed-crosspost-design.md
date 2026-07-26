# Delayed Telegram-to-MAX crossposting

## Goal

Ensure that a Telegram userbot has time to clean or edit a forwarded post
before the crossposter publishes it to MAX.

## Chosen behavior

The crossposter waits 10 seconds before publishing each non-album Telegram
channel post. During that window, an `edited_channel_post` update replaces the
pending message and restarts the timer. MAX receives only the latest Telegram
version available when the timer expires.

Media albums keep their existing batching behavior and use the same 10-second
settling window after the album batch is assembled.

## Data flow

1. Telegram sends `channel_post`.
2. The service stores it by `chat.id + message_id` and starts a 10-second
   timer.
3. Telegram sends `edited_channel_post` after the userbot removes its marker.
4. The service replaces the stored message and restarts the timer.
5. When the timer expires, the service crossposts the stored final version.

The webhook subscription must include both `channel_post` and
`edited_channel_post`.

## Failure and compatibility behavior

- Existing duplicate-update protection remains in place.
- A failed MAX publish still returns an error so Telegram can retry.
- Full deletion of a Telegram post is out of scope because this change only
  handles new and edited channel-post updates.
- The delay is an in-memory settling window; a service restart can lose a
  pending post, so the existing webhook retry behavior remains important.

## Tests

Add tests proving that:

- a new post is not published before 10 seconds;
- an edit replaces the pending text and restarts the settling timer;
- the final published text is the edited version;
- unrelated posts remain independent.
