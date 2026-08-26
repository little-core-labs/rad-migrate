"""Tests for domain model behaviour — ordering, date clamping, and filenames."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from radmigrate.media import safe_filename
from radmigrate.models import DestinationContent, ItemState, VideoItem


def make_item(**kwargs: object) -> VideoItem:
    defaults: dict[str, object] = {"source_id": "vid123", "channel_id": "UC_test"}
    defaults.update(kwargs)
    return VideoItem(**defaults)  # type: ignore[arg-type]


class TestReleaseDate:
    def test_past_date_is_preserved(self) -> None:
        item = make_item(upload_date="20240315")
        assert item.release_iso().startswith("2024-03-15")

    def test_today_is_clamped_to_the_past(self) -> None:
        """Rad rejects a release date of today or later, and a fresh upload is today."""
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        item = make_item(upload_date=today)
        assert item.release_datetime() < datetime.now(timezone.utc)

    def test_future_date_is_clamped(self) -> None:
        future = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y%m%d")
        item = make_item(upload_date=future)
        assert item.release_datetime() < datetime.now(timezone.utc)

    @pytest.mark.parametrize("bad", [None, "", "not-a-date", "2024", "20241345"])
    def test_unparseable_dates_fall_back_to_the_past(self, bad: str | None) -> None:
        item = make_item(upload_date=bad)
        assert item.release_datetime() < datetime.now(timezone.utc)
        assert item.display_date == "unknown-date"


class TestOrdering:
    def test_recency_uses_position_when_dates_are_absent(self) -> None:
        """The realistic pre-download case: YouTube's flat listing has no dates."""
        items = [make_item(source_id=f"v{i}", position=i, title=f"t{i}") for i in range(5)]
        newest_first = sorted(items, key=lambda i: i.recency_rank)
        assert [i.source_id for i in newest_first] == ["v0", "v1", "v2", "v3", "v4"]

    def test_recency_ignores_dates_on_a_minority_of_items(self) -> None:
        """A few already-downloaded items must not outrank everything else.

        This is the bug that made ``--newest 5`` return 2011 conference talks: items
        with real dates sorted into a different bucket than the dateless majority.
        """
        items = [make_item(source_id=f"v{i}", position=i) for i in range(5)]
        items[4].timestamp = 1_700_000_000  # oldest video, but the only one with a date
        newest_first = sorted(items, key=lambda i: i.recency_rank)
        assert newest_first[0].source_id == "v0"
        assert newest_first[-1].source_id == "v4"

    def test_sort_key_prefers_timestamp(self) -> None:
        older = make_item(source_id="a", timestamp=1000, upload_date="20240101")
        newer = make_item(source_id="b", timestamp=2000, upload_date="20240101")
        assert sorted([newer, older], key=lambda i: i.sort_key)[0].source_id == "a"

    def test_timestamp_breaks_same_day_ties(self) -> None:
        """Two uploads on one day share an upload_date; only timestamp orders them."""
        first = make_item(source_id="a", upload_date="20240301", timestamp=1_709_251_200)
        second = make_item(source_id="b", upload_date="20240301", timestamp=1_709_290_000)
        assert [i.source_id for i in sorted([second, first], key=lambda i: i.sort_key)] == [
            "a",
            "b",
        ]

    def test_sort_key_falls_back_to_position_oldest_first(self) -> None:
        items = [make_item(source_id=f"v{i}", position=i) for i in range(3)]
        assert [i.source_id for i in sorted(items, key=lambda i: i.sort_key)] == [
            "v2",
            "v1",
            "v0",
        ]


class TestItemState:
    @pytest.mark.parametrize(
        "state",
        [
            ItemState.FAILED_DOWNLOAD,
            ItemState.FAILED_QUALITY,
            ItemState.FAILED_PUBLISH,
            ItemState.FAILED_PROCESSING,
        ],
    )
    def test_failure_states(self, state: ItemState) -> None:
        assert state.is_failure

    def test_published_is_not_terminal_success(self) -> None:
        """Uploaded is not the same as playable — transcoding can still fail."""
        assert not ItemState.PUBLISHED.is_terminal_success
        assert ItemState.VERIFIED.is_terminal_success

    def test_quality_failure_is_retried_as_a_download(self) -> None:
        assert ItemState.FAILED_QUALITY.needs_download

    def test_verified_needs_nothing(self) -> None:
        assert not ItemState.VERIFIED.needs_download
        assert not ItemState.VERIFIED.needs_publish


class TestLandscape:
    def test_unknown_dimensions_return_none_not_a_guess(self) -> None:
        assert make_item().is_landscape is None

    def test_wide_is_landscape(self) -> None:
        assert make_item(width=1920, height=1080).is_landscape is True

    def test_tall_is_not(self) -> None:
        assert make_item(width=1080, height=1920).is_landscape is False

    def test_square_is_not_landscape(self) -> None:
        assert make_item(width=1080, height=1080).is_landscape is False


class TestDestinationMatching:
    def test_matches_on_stamped_external_id(self) -> None:
        record = DestinationContent(id="did:x", external_id="vid123")
        assert record.matches_source_id("vid123")
        assert not record.matches_source_id("other")

    def test_falls_back_to_source_url(self) -> None:
        """Covers records published before external-id stamping existed."""
        record = DestinationContent(id="did:x", source_url="https://youtube.com/watch?v=vid123")
        assert record.matches_source_id("vid123")

    def test_bare_record_matches_nothing(self) -> None:
        assert not DestinationContent(id="did:x").matches_source_id("vid123")


class TestSafeFilename:
    @pytest.mark.parametrize("char", '<>:"/\\|?*')
    def test_illegal_characters_are_replaced(self, char: str) -> None:
        assert char not in safe_filename(f"a{char}b")

    def test_uses_ascii_not_fullwidth_lookalikes(self) -> None:
        """yt-dlp substitutes ：and ｜, which then leak into logs and resist matching."""
        result = safe_filename("Dining: Barolo | Piemonte")
        assert "：" not in result
        assert "｜" not in result

    def test_windows_reserved_names_are_escaped(self) -> None:
        assert safe_filename("CON") != "CON"
        assert safe_filename("nul.txt") != "nul.txt"

    def test_empty_input_yields_a_usable_name(self) -> None:
        assert safe_filename("") == "untitled"
        assert safe_filename("   ...   ") == "untitled"

    def test_length_is_capped(self) -> None:
        assert len(safe_filename("x" * 500)) <= 120

    def test_no_trailing_dot_or_space(self) -> None:
        result = safe_filename("name. ")
        assert not result.endswith((".", " "))

    def test_unicode_titles_survive(self) -> None:
        assert "日本語" in safe_filename("日本語 video")

    def test_folder_name_includes_id_for_uniqueness(self) -> None:
        item = make_item(source_id="abc123", title="Same Title", upload_date="20240101")
        assert "abc123" in item.folder_name()
        assert "2024-01-01" in item.folder_name()
