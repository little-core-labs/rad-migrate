"""Tests for the partition step — dedupe against the destination, and selection.

This is where idempotency lives. If ``_partition`` is wrong, a re-run either
re-publishes videos that are already live or silently skips ones that are not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rad_migrate.config import Settings
from rad_migrate.models import DestinationContent, ItemState, VideoItem
from rad_migrate.pipeline import Pipeline

CHANNEL = "UC_test"


@pytest.fixture
def pipeline(tmp_path: Path) -> Pipeline:
    return Pipeline(Settings(workdir=tmp_path / "work"))


def items(count: int, **kwargs: object) -> list[VideoItem]:
    return [
        VideoItem(
            source_id=f"v{i}",
            channel_id=CHANNEL,
            title=f"Video {i}",
            position=i,
            **kwargs,  # type: ignore[arg-type]
        )
        for i in range(count)
    ]


class TestDedupe:
    def test_destination_match_marks_already_present(self, pipeline: Pipeline) -> None:
        existing = [DestinationContent(id="did:rad.live:content/feature/a", external_id="v1")]
        already, _, todo = pipeline._partition(items(3), existing, degraded=False)
        assert [i.source_id for i in already] == ["v1"]
        # todo is ordered oldest-first for publishing, so the higher position leads.
        assert [i.source_id for i in todo] == ["v2", "v0"]

    def test_dest_id_is_recorded_on_the_matched_item(self, pipeline: Pipeline) -> None:
        existing = [DestinationContent(id="did:rad.live:content/feature/a", external_id="v0")]
        already, _, _ = pipeline._partition(items(1), existing, degraded=False)
        assert already[0].dest_id == "did:rad.live:content/feature/a"
        assert already[0].state is ItemState.ALREADY_PRESENT

    def test_source_url_match_covers_unstamped_records(self, pipeline: Pipeline) -> None:
        """Records published before external-id stamping still have to be recognised."""
        existing = [
            DestinationContent(
                id="did:rad.live:content/feature/a",
                source_url="https://www.youtube.com/watch?v=v2",
            )
        ]
        already, _, todo = pipeline._partition(items(3), existing, degraded=False)
        assert [i.source_id for i in already] == ["v2"]
        assert "v2" not in [i.source_id for i in todo]

    def test_empty_destination_migrates_everything(self, pipeline: Pipeline) -> None:
        _, _, todo = pipeline._partition(items(3), [], degraded=False)
        assert len(todo) == 3

    def test_verified_state_survives_a_destination_match(self, pipeline: Pipeline) -> None:
        source = items(1)
        source[0].state = ItemState.VERIFIED
        existing = [DestinationContent(id="did:x", external_id="v0")]
        already, _, _ = pipeline._partition(source, existing, degraded=False)
        assert already[0].state is ItemState.VERIFIED


class TestDegradedReconcile:
    def test_falls_back_to_local_state(self, pipeline: Pipeline) -> None:
        """When the destination cannot be listed, published items must still be skipped."""
        source = items(3)
        source[0].state = ItemState.VERIFIED
        source[1].state = ItemState.PUBLISHED
        already, _, todo = pipeline._partition(source, [], degraded=True)
        assert {i.source_id for i in already} == {"v0", "v1"}
        assert [i.source_id for i in todo] == ["v2"]

    def test_without_degradation_local_state_does_not_skip(self, pipeline: Pipeline) -> None:
        """An authoritative empty listing means the video really is not there."""
        source = items(1)
        source[0].state = ItemState.PUBLISHED
        _, _, todo = pipeline._partition(source, [], degraded=False)
        assert [i.source_id for i in todo] == ["v0"]


class TestFilters:
    def test_duration_bounds(self, tmp_path: Path) -> None:
        pipe = Pipeline(Settings(workdir=tmp_path / "w", min_duration=60, max_duration=600))
        source = items(3)
        source[0].duration = 30
        source[1].duration = 300
        source[2].duration = 900
        _, filtered, todo = pipe._partition(source, [], degraded=False)
        assert [i.source_id for i in todo] == ["v1"]
        assert {i.skip_reason for i in filtered} == {
            "shorter than 60s",
            "longer than 600s",
        }

    def test_unknown_duration_is_not_filtered(self, tmp_path: Path) -> None:
        """Absence of data is not evidence of a short video."""
        pipe = Pipeline(Settings(workdir=tmp_path / "w", min_duration=60))
        _, _, todo = pipe._partition(items(1), [], degraded=False)
        assert len(todo) == 1

    def test_landscape_only_drops_vertical(self, tmp_path: Path) -> None:
        pipe = Pipeline(Settings(workdir=tmp_path / "w", landscape_only=True))
        source = items(2)
        source[0].width, source[0].height = 1920, 1080
        source[1].width, source[1].height = 1080, 1920
        _, filtered, todo = pipe._partition(source, [], degraded=False)
        assert [i.source_id for i in todo] == ["v0"]
        assert filtered[0].skip_reason == "vertical video"

    def test_landscape_only_keeps_unknown_dimensions(self, tmp_path: Path) -> None:
        """Dimensions are unknown before download; the real check runs then."""
        pipe = Pipeline(Settings(workdir=tmp_path / "w", landscape_only=True))
        _, _, todo = pipe._partition(items(1), [], degraded=False)
        assert len(todo) == 1

    def test_previously_skipped_items_stay_skipped(self, pipeline: Pipeline) -> None:
        source = items(2)
        source[0].state = ItemState.SKIPPED
        source[0].skip_reason = "vertical video"
        _, filtered, todo = pipeline._partition(source, [], degraded=False)
        assert [i.source_id for i in filtered] == ["v0"]
        assert [i.source_id for i in todo] == ["v1"]


class TestSelection:
    def test_newest_takes_the_top_of_the_channel(self, tmp_path: Path) -> None:
        pipe = Pipeline(Settings(workdir=tmp_path / "w", newest=3))
        _, _, todo = pipe._partition(items(10), [], degraded=False)
        assert {i.source_id for i in todo} == {"v0", "v1", "v2"}
        assert pipe._deselected == 7

    def test_publish_order_is_oldest_first(self, tmp_path: Path) -> None:
        """Publishing oldest-first keeps release dates ascending on the destination."""
        pipe = Pipeline(Settings(workdir=tmp_path / "w", newest=3))
        _, _, todo = pipe._partition(items(10), [], degraded=False)
        assert [i.source_id for i in todo] == ["v2", "v1", "v0"]

    def test_limit_caps_the_run(self, tmp_path: Path) -> None:
        pipe = Pipeline(Settings(workdir=tmp_path / "w", limit=2))
        _, _, todo = pipe._partition(items(10), [], degraded=False)
        assert len(todo) == 2

    def test_deselected_is_counted_apart_from_filtered(self, tmp_path: Path) -> None:
        """'Filtered' means rejected by a rule; 'not selected' means simply not reached."""
        pipe = Pipeline(Settings(workdir=tmp_path / "w", newest=2, min_duration=60))
        source = items(5)
        source[0].duration = 10  # genuinely filtered
        _, filtered, todo = pipe._partition(source, [], degraded=False)
        assert len(filtered) == 1
        assert len(todo) == 2
        assert pipe._deselected == 2


class TestRefresh:
    def test_position_is_taken_from_fresh_discovery(self, pipeline: Pipeline) -> None:
        """Positions shift as the channel publishes; a stored one goes stale."""
        stored = VideoItem(source_id="v1", channel_id=CHANNEL, position=0, title="Old")
        fresh = VideoItem(source_id="v1", channel_id=CHANNEL, position=3, title="New")
        merged = pipeline._refresh(stored, fresh)
        assert merged.position == 3
        assert merged.title == "New"

    def test_progress_is_preserved(self, pipeline: Pipeline) -> None:
        stored = VideoItem(
            source_id="v1",
            channel_id=CHANNEL,
            position=0,
            state=ItemState.VERIFIED,
            dest_id="did:x",
        )
        fresh = VideoItem(source_id="v1", channel_id=CHANNEL, position=1)
        merged = pipeline._refresh(stored, fresh)
        assert merged.state is ItemState.VERIFIED
        assert merged.dest_id == "did:x"

    def test_unknown_item_uses_the_fresh_record(self, pipeline: Pipeline) -> None:
        fresh = VideoItem(source_id="v9", channel_id=CHANNEL, position=9)
        assert pipeline._refresh(None, fresh) is fresh
