"""Tests for download folder naming.

The folder must be created before extraction, when the upload date is still unknown,
but the naming scheme exists to give a date-sorted layout. These cover the rename that
reconciles those two facts, and the resume behaviour that depends on it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rad_migrate.models import VideoItem
from rad_migrate.source.youtube import YouTubeSource

rename = YouTubeSource._name_folder_now_that_the_date_is_known


def item_in(folder: Path, **kwargs: object) -> VideoItem:
    defaults: dict[str, object] = {
        "source_id": "aK24bXXn",
        "channel_id": "UC_test",
        "title": "A Video",
        "folder": folder,
    }
    defaults.update(kwargs)
    return VideoItem(**defaults)  # type: ignore[arg-type]


def make_download(root: Path, name: str) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "video.mp4").write_bytes(b"video")
    (folder / "thumbnail.jpg").write_bytes(b"thumb")
    return folder


class TestFolderRename:
    def test_unknown_date_folder_is_renamed_once_the_date_arrives(self, tmp_path: Path) -> None:
        folder = make_download(tmp_path, "unknown-date - A Video [aK24bXXn]")
        item = item_in(
            folder,
            upload_date="20250929",
            video_path=folder / "video.mp4",
            thumbnail_path=folder / "thumbnail.jpg",
        )

        rename(item, tmp_path)

        assert item.folder is not None
        assert Path(item.folder).name == "2025-09-29 - A Video [aK24bXXn]"
        assert Path(item.folder).exists()
        assert not folder.exists()

    def test_recorded_paths_follow_the_rename(self, tmp_path: Path) -> None:
        """A stale path here would break the publish step immediately after."""
        folder = make_download(tmp_path, "unknown-date - A Video [aK24bXXn]")
        item = item_in(
            folder,
            upload_date="20250929",
            video_path=folder / "video.mp4",
            thumbnail_path=folder / "thumbnail.jpg",
        )

        rename(item, tmp_path)

        assert item.video_path is not None and item.video_path.exists()
        assert item.thumbnail_path is not None and item.thumbnail_path.exists()
        assert item.video_path.parent == item.folder

    def test_no_date_means_no_rename(self, tmp_path: Path) -> None:
        folder = make_download(tmp_path, "unknown-date - A Video [aK24bXXn]")
        item = item_in(folder, upload_date=None)
        rename(item, tmp_path)
        assert item.folder == folder

    def test_already_correct_name_is_left_alone(self, tmp_path: Path) -> None:
        folder = make_download(tmp_path, "2025-09-29 - A Video [aK24bXXn]")
        item = item_in(folder, upload_date="20250929")
        rename(item, tmp_path)
        assert item.folder == folder
        assert folder.exists()

    def test_collision_leaves_the_original_intact(self, tmp_path: Path) -> None:
        """Cosmetic only — never destroy a download to tidy a name."""
        folder = make_download(tmp_path, "unknown-date - A Video [aK24bXXn]")
        (tmp_path / "2025-09-29 - A Video [aK24bXXn]").mkdir()
        item = item_in(folder, upload_date="20250929", video_path=folder / "video.mp4")

        rename(item, tmp_path)

        assert item.folder == folder
        assert item.video_path is not None and item.video_path.exists()

    @pytest.mark.parametrize("title", ["A: B", "A/B", "A|B", 'A"B', "A*B?"])
    def test_titles_with_illegal_characters_still_rename(self, tmp_path: Path, title: str) -> None:
        folder = make_download(tmp_path, "unknown-date - safe [aK24bXXn]")
        item = item_in(folder, title=title, upload_date="20250929")
        rename(item, tmp_path)
        assert item.folder is not None and Path(item.folder).exists()


class TestResume:
    def test_a_stored_folder_is_reused(self, tmp_path: Path) -> None:
        """After a partial download the date may now be known, which would compute a
        different name — and abandon the bytes already on disk."""
        stored = tmp_path / "unknown-date - A Video [aK24bXXn]"
        stored.mkdir()
        item = item_in(stored, upload_date="20250929")
        assert item.folder == stored
        assert item.folder_name() != stored.name
