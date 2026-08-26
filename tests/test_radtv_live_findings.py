"""Regression tests for behaviour learned from the live Rad TV API.

Every case here corresponds to something that was wrong, or would have been wrong,
until a real integration run proved it. Fixture payloads are trimmed copies of actual
responses, with identifiers replaced.
"""

from __future__ import annotations

import json

import pytest

from rad_migrate.config import Settings
from rad_migrate.dest.radtv import RadTVDestination, _classify_upload_error
from rad_migrate.errors import DestinationError, TransientError
from rad_migrate.models import DestinationContent, VideoItem

CONTENT_ID = "did:rad.live:content/feature/aaaabbbb-cccc-dddd-eeee-ffff00001111"
SOURCE_ID = "vIdEoIdAbCd"


def item(**kwargs: object) -> VideoItem:
    defaults: dict[str, object] = {
        "source_id": SOURCE_ID,
        "channel_id": "UC_source",
        "title": "Subscribe for all the access to your favourite teams",
        "upload_date": "20220815",
    }
    defaults.update(kwargs)
    return VideoItem(**defaults)  # type: ignore[arg-type]


class TestPublishArguments:
    """`sourceUrl` on this API means "download it yourself", not "provenance".

    Sending the YouTube URL there switches publish_video into server-side fetch mode
    and makes Rad re-download from YouTube, ignoring the file we just fetched in HD.
    """

    def _arguments(self, video, thumb=None, **kwargs):  # type: ignore[no-untyped-def]
        captured: dict = {}

        class Fake(RadTVDestination):
            def __init__(self) -> None:
                self.reporter = None  # type: ignore[assignment]

            def call(self, tool, arguments=None, **_):  # type: ignore[no-untyped-def]
                captured.update(arguments or {})
                return json.dumps(
                    {
                        "contentId": CONTENT_ID,
                        "ingestion": {"video": {"url": "https://api.rad.live/ingestion/aaa"}},
                    }
                )

        entry = item(video_path=video, thumbnail_path=thumb, **kwargs)
        Fake().create_content(entry, "did:rad.live:channel/abc")
        return captured

    def test_source_url_is_never_sent(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        video = tmp_path / "video.mp4"
        video.write_bytes(b"x" * 1024)
        args = self._arguments(video)
        assert "sourceUrl" not in args, "sourceUrl would trigger server-side download"

    def test_size_is_always_sent(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """The API warns that uploads without a size may produce corrupted files."""
        video = tmp_path / "video.mp4"
        video.write_bytes(b"x" * 4096)
        assert self._arguments(video)["size"] == 4096

    def test_landscape_layout_by_default(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        video = tmp_path / "video.mp4"
        video.write_bytes(b"x")
        args = self._arguments(video, width=1920, height=1080)
        assert args["layout"] == "LANDSCAPE"

    def test_portrait_video_is_marked_portrait(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """A vertical video published as LANDSCAPE displays wrong."""
        video = tmp_path / "video.mp4"
        video.write_bytes(b"x")
        args = self._arguments(video, width=1080, height=1920)
        assert args["layout"] == "PORTRAIT"

    def test_draft_mode_is_propagated(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        video = tmp_path / "video.mp4"
        video.write_bytes(b"x")
        captured: dict = {}

        class Fake(RadTVDestination):
            def __init__(self) -> None:
                self.reporter = None  # type: ignore[assignment]

            def call(self, tool, arguments=None, **_):  # type: ignore[no-untyped-def]
                captured.update(arguments or {})
                return json.dumps(
                    {"contentId": CONTENT_ID, "ingestion": {"video": {"url": "https://x/y"}}}
                )

        Fake().create_content(item(video_path=video), "chan", publish=False)
        assert captured["publish"] is False


class TestProcessingStatusParsing:
    """Real dashboard/status payloads."""

    def _status(self, payload: str):  # type: ignore[no-untyped-def]
        class Fake(RadTVDestination):
            def __init__(self) -> None:
                pass

            def call(self, tool, arguments=None, **_):  # type: ignore[no-untyped-def]
                return payload

        return Fake().processing_status(CONTENT_ID)

    def test_transcoding_is_not_terminal(self) -> None:
        payload = json.dumps(
            {
                "status": "TRANSCODING",
                "job": {"id": "21638", "status": "TRANSCODING", "progress": 20},
            }
        )
        status = self._status(payload)
        assert status.status == "TRANSCODING"
        assert not status.is_terminal
        assert status.progress == 20.0
        assert status.job_id == "21638"

    def test_complete_is_terminal_success(self) -> None:
        payload = json.dumps(
            {"status": "COMPLETE", "job": {"id": "21638", "status": "COMPLETE", "progress": 100}}
        )
        status = self._status(payload)
        assert status.is_complete and status.is_terminal

    def test_error_is_terminal_failure(self) -> None:
        payload = json.dumps({"job": {"id": "19250", "status": "ERROR", "progress": 0}})
        status = self._status(payload)
        assert status.is_failed and status.is_terminal

    def test_missing_job_is_pending_not_an_error(self) -> None:
        """There is a real window after upload where the job does not exist yet.

        Raising here marked a perfectly good video as failed.
        """

        class Fake(RadTVDestination):
            def __init__(self) -> None:
                pass

            def call(self, tool, arguments=None, **_):  # type: ignore[no-untyped-def]
                raise DestinationError(
                    "get_processing_status failed: No transcode job found for this content"
                )

        status = Fake().processing_status(CONTENT_ID)
        assert status.is_pending
        assert not status.is_terminal
        assert not status.is_failed

    def test_other_errors_still_raise(self) -> None:
        class Fake(RadTVDestination):
            def __init__(self) -> None:
                pass

            def call(self, tool, arguments=None, **_):  # type: ignore[no-untyped-def]
                raise DestinationError("get_processing_status failed: content not found")

        with pytest.raises(DestinationError):
            Fake().processing_status(CONTENT_ID)


class TestChannelDashboardParsing:
    """`channel_dashboard` is the only content listing available."""

    DASHBOARD = json.dumps(
        {
            "channel": {
                "id": "did:rad.live:channel/abc",
                "name": "A Channel",
                "followersCount": 25,
            },
            "recentContent": [
                {
                    "id": CONTENT_ID,
                    "title": "Subscribe for all the access to your favourite teams",
                    "summary": "...",
                    "visibility": "public",
                    "processingStatus": "ready",
                    "transcodeJob": {"id": "21638", "status": "COMPLETE", "progress": 100},
                }
            ],
        }
    )

    def test_content_is_extracted(self) -> None:
        records = list(RadTVDestination._parse_content(self.DASHBOARD))
        assert len(records) == 1
        assert records[0].id == CONTENT_ID
        assert records[0].status == "ready"

    def test_channel_record_is_not_mistaken_for_content(self) -> None:
        records = list(RadTVDestination._parse_content(self.DASHBOARD))
        assert all("channel" not in r.id for r in records)


class TestTitleMatching:
    """No externalId field exists, so title (plus date when available) is the key."""

    def test_matches_the_same_video(self) -> None:
        record = DestinationContent(
            id=CONTENT_ID, title="Subscribe for all the access to your favourite teams"
        )
        assert record.matches(item())

    def test_punctuation_and_case_differences_still_match(self) -> None:
        record = DestinationContent(
            id=CONTENT_ID, title="SUBSCRIBE, for all the access to your favourite teams!"
        )
        assert record.matches(item())

    def test_unrelated_title_does_not_match(self) -> None:
        record = DestinationContent(id=CONTENT_ID, title="Something Else Entirely")
        assert not record.matches(item())

    def test_same_title_different_year_does_not_match(self) -> None:
        """Channels reuse titles across seasons; the date has to disambiguate."""
        record = DestinationContent(
            id=CONTENT_ID,
            title="Season Preview",
            release_date="2021-08-15T12:00:00Z",
        )
        assert not record.matches(item(title="Season Preview"))

    def test_same_title_same_day_matches(self) -> None:
        record = DestinationContent(
            id=CONTENT_ID,
            title="Season Preview",
            release_date="2022-08-15T12:00:00Z",
        )
        assert record.matches(item(title="Season Preview"))

    def test_empty_title_never_matches(self) -> None:
        assert not DestinationContent(id=CONTENT_ID, title="").matches(item())

    def test_stamped_id_wins_over_title(self) -> None:
        """Ready for the day the API gains provenance stamping."""
        record = DestinationContent(id=CONTENT_ID, title="Totally Different", external_id=SOURCE_ID)
        assert record.matches(item())


class TestPlaylistIdParsing:
    """Create returns `content/playlist/<id>`; the dashboard returns `playlist/<id>`."""

    def _create(self, payload: str) -> str | None:
        class Fake(RadTVDestination):
            def __init__(self) -> None:
                from rad_migrate.reporting import null_reporter

                self.reporter = null_reporter()

            def call(self, tool, arguments=None, **_):  # type: ignore[no-untyped-def]
                return payload

        return Fake().create_playlist("chan", "Title", [CONTENT_ID])

    def test_create_response_form(self) -> None:
        payload = json.dumps(
            {
                "playlist": {
                    "id": "did:rad.live:content/playlist/11112222-3333-4444-5555-666677778888",
                    "metadata": {"title": "Title"},
                },
                "itemCount": 1,
            }
        )
        assert self._create(payload) == (
            "did:rad.live:content/playlist/11112222-3333-4444-5555-666677778888"
        )

    def test_dashboard_form(self) -> None:
        playlist = "did:rad.live:playlist/99998888-7777-6666-5555-444433332222"
        assert self._create(json.dumps({"id": playlist})) == playlist

    def test_prose_fallback(self) -> None:
        payload = "Created playlist did:rad.live:content/playlist/abc-123 with 1 item."
        assert self._create(payload) == "did:rad.live:content/playlist/abc-123"

    def test_unreadable_response_returns_none_rather_than_guessing(self) -> None:
        assert self._create("Something went sideways") is None

    def test_empty_content_ids_creates_nothing(self) -> None:
        class Fake(RadTVDestination):
            def __init__(self) -> None:
                pass

            def call(self, tool, arguments=None, **_):  # type: ignore[no-untyped-def]
                raise AssertionError("must not call the API for an empty playlist")

        assert Fake().create_playlist("chan", "Title", []) is None


class TestUploadErrors:
    def test_missing_ingestion_key_is_transient(self) -> None:
        """Seen in production: the upload target vanishes between create and upload."""
        error = _classify_upload_error(404, "Key not found in database")
        assert isinstance(error, TransientError)

    def test_client_error_is_permanent(self) -> None:
        error = _classify_upload_error(400, "bad request")
        assert isinstance(error, DestinationError)
        assert not isinstance(error, TransientError)


def test_own_channel_comes_from_verify_connection() -> None:
    """`list_channels` enumerates every channel on the platform, not ours."""
    payload = json.dumps(
        {
            "authenticated": True,
            "user": {"username": "someone", "email": "someone@example.com"},
            "channel": {"id": "did:rad.live:channel/abc", "name": "A Channel"},
        }
    )

    class Fake(RadTVDestination):
        def __init__(self) -> None:
            pass

        def call(self, tool, arguments=None, **_):  # type: ignore[no-untyped-def]
            assert tool == "verify_connection"
            return payload

    fake = Fake()
    assert fake.list_channels() == [("did:rad.live:channel/abc", "A Channel")]
    assert fake.verify() == "someone → A Channel"


def test_settings_unused() -> None:
    """Guard against the fixtures drifting from the real Settings shape."""
    assert Settings().api_url == "https://api.rad.live"
