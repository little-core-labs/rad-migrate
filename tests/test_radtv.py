"""Tests for Rad TV response handling.

The publish response is the fragile spot: today it may be prose that has to be
scraped, and the API is expected to gain a structured form. Both paths are tested,
along with the precedence between them, so the switchover is a non-event.
"""

from __future__ import annotations

import json

import pytest

from rad_migrate.dest.radtv import RadTVDestination, _summary_for
from rad_migrate.errors import DestinationError

CONTENT_ID = "did:rad.live:content/feature/8b1f4c2e-0000-4aaa-9bbb-1234567890ab"
VIDEO_URL = "https://api.rad.live/ingestion/abc123def456"
THUMB_URL = "https://api.rad.live/ingestion/thumb789xyz"


def parse(text: str, *, want_thumbnail: bool = True):  # type: ignore[no-untyped-def]
    return RadTVDestination._parse_publish(text, want_thumbnail=want_thumbnail)


class TestPublishParsingProse:
    """Today's shape: a human-readable blob that has to be scraped."""

    def test_extracts_id_and_urls(self) -> None:
        text = (
            f"Created content record.\nContent ID: {CONTENT_ID}\n"
            f"Upload the video to: {VIDEO_URL}\n"
            f"Upload the thumbnail to: {THUMB_URL}\n"
        )
        target = parse(text)
        assert target.content_id == CONTENT_ID
        assert target.video_upload_url == VIDEO_URL
        assert target.thumbnail_upload_url == THUMB_URL

    def test_video_url_comes_first_by_convention(self) -> None:
        text = f"Content ID: {CONTENT_ID}\n{VIDEO_URL}\n{THUMB_URL}"
        assert parse(text).video_upload_url == VIDEO_URL

    def test_no_thumbnail_url_when_none_was_sent(self) -> None:
        text = f"Content ID: {CONTENT_ID}\n{VIDEO_URL}\n{THUMB_URL}"
        assert parse(text, want_thumbnail=False).thumbnail_upload_url is None

    def test_trailing_punctuation_is_not_captured(self) -> None:
        text = f'Content ID: {CONTENT_ID}\nPUT to "{VIDEO_URL}".'
        assert parse(text).video_upload_url == VIDEO_URL

    def test_bare_content_id_without_label(self) -> None:
        assert parse(f"{CONTENT_ID} ready, upload to {VIDEO_URL}").content_id == CONTENT_ID

    def test_missing_upload_url_is_an_error(self) -> None:
        with pytest.raises(DestinationError, match="content id and upload URL"):
            parse(f"Content ID: {CONTENT_ID}")

    def test_missing_content_id_is_an_error(self) -> None:
        with pytest.raises(DestinationError):
            parse(f"Upload to {VIDEO_URL}")


class TestPublishParsingStructured:
    """The shape the API should return. Preferred whenever present."""

    def test_nested_ingestion_object(self) -> None:
        payload = json.dumps(
            {
                "contentId": CONTENT_ID,
                "ingestion": {"video": {"url": VIDEO_URL}, "thumbnail": {"url": THUMB_URL}},
            }
        )
        target = parse(payload)
        assert target.content_id == CONTENT_ID
        assert target.video_upload_url == VIDEO_URL
        assert target.thumbnail_upload_url == THUMB_URL

    def test_ingestion_values_as_plain_strings(self) -> None:
        payload = json.dumps(
            {"contentId": CONTENT_ID, "ingestion": {"video": VIDEO_URL, "thumbnail": THUMB_URL}}
        )
        assert parse(payload).video_upload_url == VIDEO_URL

    def test_flat_upload_url_keys(self) -> None:
        payload = json.dumps(
            {
                "contentId": CONTENT_ID,
                "uploadUrl": VIDEO_URL,
                "thumbnailUploadUrl": THUMB_URL,
            }
        )
        assert parse(payload).thumbnail_upload_url == THUMB_URL

    def test_json_embedded_in_prose(self) -> None:
        payload = f"Done. {json.dumps({'contentId': CONTENT_ID, 'uploadUrl': VIDEO_URL})}"
        assert parse(payload).content_id == CONTENT_ID

    def test_structured_wins_over_prose(self) -> None:
        """A stale id mentioned in surrounding prose must not shadow the real one."""
        other = "did:rad.live:content/feature/ffffffff-1111-2222-3333-444444444444"
        payload = f"Replacing {other}. " + json.dumps(
            {"contentId": CONTENT_ID, "ingestion": {"video": {"url": VIDEO_URL}}}
        )
        assert parse(payload).content_id == CONTENT_ID


class TestRetryClassification:
    @pytest.mark.parametrize(
        "message",
        [
            "Response ended prematurely",
            "gateway timeout",
            "Internal Server Error",
            "connection reset by peer",
            "Too Many Requests",
        ],
    )
    def test_transient_messages_retry(self, message: str) -> None:
        assert RadTVDestination._should_retry(RuntimeError(message))

    @pytest.mark.parametrize(
        "message",
        ["Unauthorized", "invalid api key", "Forbidden"],
    )
    def test_auth_messages_do_not_retry(self, message: str) -> None:
        """Retrying a rejected key just burns time and rate limit."""
        assert not RadTVDestination._should_retry(RuntimeError(message))

    def test_unknown_errors_do_not_retry(self) -> None:
        assert not RadTVDestination._should_retry(RuntimeError("title too long"))


class TestSummary:
    def test_short_description_passes_through(self) -> None:
        from rad_migrate.models import VideoItem

        item = VideoItem(source_id="v", channel_id="c", description="Short.")
        assert _summary_for(item) == "Short."

    def test_long_description_is_trimmed_on_a_boundary(self) -> None:
        from rad_migrate.models import VideoItem

        body = ("Sentence number one. " * 400).strip()
        item = VideoItem(source_id="v", channel_id="c", description=body)
        result = _summary_for(item)
        assert len(result) <= 4001
        assert result.endswith("…")

    def test_missing_description_is_empty(self) -> None:
        from rad_migrate.models import VideoItem

        assert _summary_for(VideoItem(source_id="v", channel_id="c")) == ""
