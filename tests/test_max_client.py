import httpx
import pytest

from app.max_client import (
    AmbiguousMaxSendError,
    MaxClient,
    MediaLimitError,
    validate_media_metadata,
)


@pytest.fixture
def media_file(tmp_path):
    path = tmp_path / "edit.mp4"
    path.write_bytes(b"large-video-content")
    return path


async def test_upload_uses_slot_and_streaming_multipart(media_file):
    seen_upload = {}

    async def handler(request):
        if request.url.path == "/uploads":
            assert request.url.params["type"] == "video"
            assert request.headers["authorization"] == "max-token"
            return httpx.Response(
                200,
                json={"url": "https://upload.example/video", "token": "slot-token"},
            )
        if request.url.host == "upload.example":
            seen_upload["content_type"] = request.headers["content-type"]
            seen_upload["body"] = await request.aread()
            return httpx.Response(200, json={"token": "uploaded-video"})
        raise AssertionError(f"Unexpected request {request.url}")

    client = MaxClient(
        token="max-token",
        chat_id="-123",
        transport=httpx.MockTransport(handler),
    )
    try:
        attachment = await client.upload(media_file, "video")
    finally:
        await client.close()

    assert attachment == {"type": "video", "payload": {"token": "uploaded-video"}}
    assert seen_upload["content_type"].startswith("multipart/form-data; boundary=")
    assert b"large-video-content" in seen_upload["body"]


async def test_image_upload_preserves_photo_payload(tmp_path):
    path = tmp_path / "image.jpg"
    path.write_bytes(b"jpeg")

    async def handler(request):
        if request.url.path == "/uploads":
            return httpx.Response(200, json={"url": "https://upload.example/image"})
        await request.aread()
        return httpx.Response(200, json={"photos": {"photo-id": {"token": "photo-token"}}})

    client = MaxClient(token="token", chat_id="1", transport=httpx.MockTransport(handler))
    try:
        attachment = await client.upload(path, "image")
    finally:
        await client.close()
    assert attachment == {
        "type": "image",
        "payload": {"photos": {"photo-id": {"token": "photo-token"}}},
    }


async def test_send_retries_only_attachment_not_ready_and_disables_preview():
    attempts = 0
    delays = []

    async def handler(request):
        nonlocal attempts
        attempts += 1
        assert request.url.params["chat_id"] == "-123"
        assert request.url.params["disable_link_preview"] == "true"
        payload = __import__("json").loads((await request.aread()).decode())
        assert payload["format"] == "html"
        if attempts == 1:
            return httpx.Response(
                400,
                json={
                    "success": False,
                    "code": "attachment.not.ready",
                    "message": "wait",
                },
            )
        return httpx.Response(200, json={"message": {"id": "max-77"}})

    async def fake_sleep(seconds):
        delays.append(seconds)

    client = MaxClient(
        token="token",
        chat_id="-123",
        transport=httpx.MockTransport(handler),
        sleep=fake_sleep,
    )
    try:
        message_id = await client.send("<b>Пост</b>", [{"type": "video"}])
    finally:
        await client.close()

    assert message_id == "max-77"
    assert attempts == 2
    assert delays == [1]


async def test_network_failure_during_send_is_ambiguous_and_not_retried():
    attempts = 0

    async def handler(request):
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("response lost", request=request)

    client = MaxClient(token="token", chat_id="1", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(AmbiguousMaxSendError):
            await client.send("Пост", [])
    finally:
        await client.close()
    assert attempts == 1


async def test_preflight_enforces_max_video_limit(tmp_path):
    path = tmp_path / "too-large.mp4"
    with path.open("wb") as stream:
        stream.truncate(250 * 1024 * 1024 + 1)
    client = MaxClient(token="token", chat_id="1")
    try:
        with pytest.raises(MediaLimitError, match="250"):
            client.validate_media(path, "video")
    finally:
        await client.close()


def test_preflight_rejects_audio_over_sixty_minutes_before_download():
    with pytest.raises(MediaLimitError, match="60"):
        validate_media_metadata({"kind": "audio", "size": 1024, "duration": 3601})
