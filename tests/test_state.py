"""Tests for the SQLite resume journal.

The property that matters: discovery runs on every invocation and must never reset an
item that is already downloaded or published.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from radmigrate.models import RETRYABLE, ItemState, VideoItem
from radmigrate.state import Store

CHANNEL = "UC_test_channel"


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "state.db")


def item(source_id: str, **kwargs: object) -> VideoItem:
    defaults: dict[str, object] = {"source_id": source_id, "channel_id": CHANNEL}
    defaults.update(kwargs)
    return VideoItem(**defaults)  # type: ignore[arg-type]


class TestRoundTrip:
    def test_stores_and_reads_back_all_fields(self, store: Store, tmp_path: Path) -> None:
        original = item(
            "v1",
            title="A Video",
            upload_date="20240101",
            timestamp=1_704_110_400,
            duration=125,
            width=1920,
            height=1080,
            position=3,
            video_path=tmp_path / "video.mp4",
            dest_id="did:rad.live:content/feature/x",
            state=ItemState.PUBLISHED,
        )
        store.upsert(original)

        loaded = store.get(CHANNEL, "v1")
        assert loaded is not None
        assert loaded.title == "A Video"
        assert loaded.duration == 125
        assert loaded.position == 3
        assert loaded.video_path == tmp_path / "video.mp4"
        assert loaded.state is ItemState.PUBLISHED
        assert loaded.dest_id == "did:rad.live:content/feature/x"

    def test_missing_item_is_none(self, store: Store) -> None:
        assert store.get(CHANNEL, "nope") is None

    def test_upsert_overwrites(self, store: Store) -> None:
        store.upsert(item("v1", state=ItemState.DISCOVERED))
        store.upsert(item("v1", state=ItemState.VERIFIED))
        loaded = store.get(CHANNEL, "v1")
        assert loaded is not None and loaded.state is ItemState.VERIFIED


class TestAddIfAbsent:
    def test_inserts_new_items(self, store: Store) -> None:
        assert store.add_if_absent([item("v1"), item("v2")]) == 2
        assert store.known_ids(CHANNEL) == {"v1", "v2"}

    def test_does_not_reset_existing_progress(self, store: Store) -> None:
        """Rediscovery must not knock a published video back to 'discovered'."""
        store.upsert(item("v1", state=ItemState.VERIFIED, dest_id="did:x"))
        store.add_if_absent([item("v1", state=ItemState.DISCOVERED)])
        loaded = store.get(CHANNEL, "v1")
        assert loaded is not None
        assert loaded.state is ItemState.VERIFIED
        assert loaded.dest_id == "did:x"

    def test_empty_input_is_a_noop(self, store: Store) -> None:
        assert store.add_if_absent([]) == 0

    def test_channels_are_isolated(self, store: Store) -> None:
        store.upsert(item("v1"))
        store.upsert(VideoItem(source_id="v1", channel_id="UC_other"))
        assert store.known_ids(CHANNEL) == {"v1"}
        assert store.known_ids("UC_other") == {"v1"}
        assert set(store.channel_ids()) == {CHANNEL, "UC_other"}


class TestQueries:
    def test_counts_by_state(self, store: Store) -> None:
        store.upsert_many(
            [
                item("v1", state=ItemState.VERIFIED),
                item("v2", state=ItemState.VERIFIED),
                item("v3", state=ItemState.FAILED_DOWNLOAD),
            ]
        )
        assert store.counts(CHANNEL) == {"verified": 2, "failed_download": 1}

    def test_items_in_states_is_sorted_chronologically(self, store: Store) -> None:
        store.upsert_many(
            [
                item("v1", state=ItemState.DISCOVERED, timestamp=3000),
                item("v2", state=ItemState.DISCOVERED, timestamp=1000),
                item("v3", state=ItemState.VERIFIED, timestamp=2000),
            ]
        )
        found = store.items_in(CHANNEL, [ItemState.DISCOVERED])
        assert [i.source_id for i in found] == ["v2", "v1"]

    def test_items_in_no_states(self, store: Store) -> None:
        assert store.items_in(CHANNEL, []) == []


class TestResetStates:
    def test_download_failures_return_to_discovered(self, store: Store) -> None:
        store.upsert(item("v1", state=ItemState.FAILED_DOWNLOAD))
        assert store.reset_states(CHANNEL, list(RETRYABLE)) == 1
        loaded = store.get(CHANNEL, "v1")
        assert loaded is not None and loaded.state is ItemState.DISCOVERED

    def test_processing_failures_return_to_published(self, store: Store) -> None:
        """The bytes are already uploaded; only the transcode needs re-triggering."""
        store.upsert(item("v1", state=ItemState.FAILED_PROCESSING, dest_id="did:x"))
        store.reset_states(CHANNEL, list(RETRYABLE))
        loaded = store.get(CHANNEL, "v1")
        assert loaded is not None and loaded.state is ItemState.PUBLISHED

    def test_publish_failure_without_media_redownloads(self, store: Store) -> None:
        store.upsert(item("v1", state=ItemState.FAILED_PUBLISH, video_path=None))
        store.reset_states(CHANNEL, list(RETRYABLE))
        loaded = store.get(CHANNEL, "v1")
        assert loaded is not None and loaded.state is ItemState.DISCOVERED

    def test_publish_failure_with_media_retries_publish(self, store: Store, tmp_path: Path) -> None:
        media = tmp_path / "video.mp4"
        media.write_bytes(b"x")
        store.upsert(item("v1", state=ItemState.FAILED_PUBLISH, video_path=media))
        store.reset_states(CHANNEL, list(RETRYABLE))
        loaded = store.get(CHANNEL, "v1")
        assert loaded is not None and loaded.state is ItemState.DOWNLOADED

    def test_successes_are_untouched(self, store: Store) -> None:
        store.upsert(item("v1", state=ItemState.VERIFIED))
        assert store.reset_states(CHANNEL, list(RETRYABLE)) == 0

    def test_error_is_cleared_on_reset(self, store: Store) -> None:
        store.upsert(item("v1", state=ItemState.FAILED_DOWNLOAD, last_error="boom"))
        store.reset_states(CHANNEL, list(RETRYABLE))
        loaded = store.get(CHANNEL, "v1")
        assert loaded is not None and loaded.last_error is None


class TestJobs:
    def test_lifecycle(self, store: Store) -> None:
        job_id = store.create_job("@channel", {"limit": 5})
        job = store.get_job(job_id)
        assert job is not None
        assert job["status"] == "running"
        assert job["request"] == {"limit": 5}

        store.finish_job(job_id, status="completed", result={"published": 5})
        done = store.get_job(job_id)
        assert done is not None
        assert done["status"] == "completed"
        assert done["result"] == {"published": 5}

    def test_cancel_sets_the_flag_the_worker_polls(self, store: Store) -> None:
        job_id = store.create_job("@channel", {})
        assert not store.job_is_cancelling(job_id)
        assert store.cancel_job(job_id)
        assert store.job_is_cancelling(job_id)

    def test_cannot_cancel_a_finished_job(self, store: Store) -> None:
        job_id = store.create_job("@channel", {})
        store.finish_job(job_id, status="completed")
        assert not store.cancel_job(job_id)

    def test_unknown_job_is_none(self, store: Store) -> None:
        assert store.get_job("nope") is None

    def test_failure_is_recorded(self, store: Store) -> None:
        job_id = store.create_job("@channel", {})
        store.finish_job(job_id, status="failed", error="no api key")
        job = store.get_job(job_id)
        assert job is not None and job["error"] == "no api key"


def test_reopening_preserves_data(tmp_path: Path) -> None:
    """The point of the journal: a killed process resumes where it stopped."""
    path = tmp_path / "state.db"
    Store(path).upsert(item("v1", state=ItemState.DOWNLOADED))
    reopened = Store(path)
    loaded = reopened.get(CHANNEL, "v1")
    assert loaded is not None and loaded.state is ItemState.DOWNLOADED
