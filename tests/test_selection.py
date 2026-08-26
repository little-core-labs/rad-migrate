"""Tests for format selection, quality strategy, and channel URL normalisation."""

from __future__ import annotations

import pytest

from radmigrate.config import QualityStrategy, Settings
from radmigrate.errors import ConfigError, SourceError
from radmigrate.source.youtube import (
    build_format,
    classify_error,
    is_permanent,
    normalize_channel_url,
    validate_ydl_opts,
)


class TestBuildFormat:
    def test_strict_has_no_rung_below_the_minimum(self) -> None:
        """The whole point of hd-strict: fail rather than silently save SD."""
        selector = build_format(QualityStrategy.HD_STRICT, 720)
        for rung in selector.split("/"):
            assert "height>=720" in rung, f"{rung!r} would accept sub-720p"

    def test_strict_keeps_an_hls_rung(self) -> None:
        """From a flagged IP the DASH HD streams 403 but m3u8 still serves HD."""
        assert "m3u8_native" in build_format(QualityStrategy.HD_STRICT, 720)

    def test_preferred_falls_back_below_the_minimum(self) -> None:
        selector = build_format(QualityStrategy.HD_PREFERRED, 720)
        assert selector.endswith("best")
        assert any("height>=" not in rung for rung in selector.split("/"))

    def test_best_is_unconstrained(self) -> None:
        assert "height" not in build_format(QualityStrategy.BEST, 720)

    @pytest.mark.parametrize("height", [480, 720, 1080, 2160])
    def test_minimum_is_threaded_through(self, height: int) -> None:
        assert f"height>={height}" in build_format(QualityStrategy.HD_STRICT, height)

    def test_prefers_mp4_and_m4a_first(self) -> None:
        """Most compatible with downstream transcoding."""
        assert build_format(QualityStrategy.HD_STRICT, 720).startswith(
            "bestvideo[height>=720][ext=mp4]+bestaudio[ext=m4a]"
        )


class TestNormalizeChannelUrl:
    @pytest.mark.parametrize(
        "raw",
        [
            "@mychannel",
            "mychannel",
            "https://www.youtube.com/@mychannel",
            "https://youtube.com/@mychannel/",
            "youtube.com/@mychannel",
            "https://www.youtube.com/@mychannel/videos",
            "https://www.youtube.com/@mychannel/featured",
        ],
    )
    def test_all_forms_land_on_the_videos_tab(self, raw: str) -> None:
        assert normalize_channel_url(raw).endswith("/@mychannel/videos")

    def test_channel_id_form(self) -> None:
        channel_id = "UCXuqSBlHAE6Xw-yeJA0Tunw"
        assert normalize_channel_url(channel_id).endswith(f"/channel/{channel_id}/videos")

    def test_legacy_c_and_user_paths(self) -> None:
        assert normalize_channel_url("https://youtube.com/c/Name").endswith("/c/Name/videos")
        assert normalize_channel_url("https://youtube.com/user/Name").endswith("/user/Name/videos")

    def test_shorts_tab_is_rewritten_to_videos(self) -> None:
        assert normalize_channel_url("https://youtube.com/@x/shorts").endswith("/@x/videos")

    @pytest.mark.parametrize(
        "raw",
        [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://www.youtube.com/playlist?list=PL123",
        ],
    )
    def test_video_and_playlist_urls_are_rejected(self, raw: str) -> None:
        with pytest.raises(SourceError, match="not a channel"):
            normalize_channel_url(raw)

    def test_empty_input_is_rejected(self) -> None:
        with pytest.raises(SourceError):
            normalize_channel_url("   ")


class TestErrorClassification:
    @pytest.mark.parametrize(
        "message",
        [
            "ERROR: Sign in to confirm you're not a bot",
            "HTTP Error 429: Too Many Requests",
            "The uploader has rate-limited this request",
            "Unable to download API page",
        ],
    )
    def test_throttling_is_recognised(self, message: str) -> None:
        from radmigrate.errors import RateLimitError

        assert classify_error(message) is RateLimitError

    @pytest.mark.parametrize(
        "message",
        [
            "Private video. Sign in if you've been granted access",
            "Video unavailable",
            "This video has been removed by the uploader",
            "Join this channel to get access to members-only content",
        ],
    )
    def test_permanent_failures_are_recognised(self, message: str) -> None:
        """These must be skipped, not retried forever."""
        assert is_permanent(message)

    def test_ordinary_errors_are_neither(self) -> None:
        assert not is_permanent("Unable to write to disk")
        assert classify_error("Unable to write to disk") is SourceError


class TestValidateYdlOpts:
    def test_accepts_the_options_we_actually_use(self) -> None:
        opts = {
            "format": "best",
            "cookiefile": "x",
            "sleep_interval_requests": 2,
            "extractor_args": {},
            "js_runtimes": {},
            "writeinfojson": True,
            "match_filter": None,
            "paths": {},
            "outtmpl": {},
        }
        assert validate_ydl_opts(opts) == []

    def test_flags_names_yt_dlp_would_silently_ignore(self) -> None:
        """The exact class of bug this guard exists for.

        An earlier script passed these four names; yt-dlp ignores unknown keys
        without complaint, so its advertised rate limiting never ran.
        """
        bogus = {
            "sleep_interval_min": 1,
            "sleep_interval_max": 2,
            "sleep_interval_requests_min": 3,
            "writetranscript": True,
        }
        assert sorted(validate_ydl_opts(bogus)) == [
            "sleep_interval_max",
            "sleep_interval_min",
            "sleep_interval_requests_min",
            "writetranscript",
        ]


class TestSettingsValidation:
    def test_contradictory_durations_are_rejected(self) -> None:
        settings = Settings(min_duration=600, max_duration=60)
        with pytest.raises(ConfigError, match="nothing can match"):
            settings.validate_selection()

    def test_missing_cookie_file_is_rejected(self, tmp_path: object) -> None:
        settings = Settings(cookies_file=tmp_path / "nope.txt")  # type: ignore[operator]
        with pytest.raises(ConfigError, match="not found"):
            settings.validate_selection()

    def test_cookie_sources_are_mutually_exclusive(self, tmp_path: object) -> None:
        cookies = tmp_path / "c.txt"  # type: ignore[operator]
        cookies.write_text("# Netscape HTTP Cookie File\n")
        settings = Settings(cookies_file=cookies, cookies_from_browser="firefox")
        with pytest.raises(ConfigError, match="mutually exclusive"):
            settings.validate_selection()

    def test_unknown_browser_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown browser"):
            Settings(cookies_from_browser="netscape")

    def test_browser_profile_suffix_is_allowed(self) -> None:
        assert Settings(cookies_from_browser="chrome:Profile 2").cookies_from_browser

    def test_allow_sd_disables_the_quality_floor(self) -> None:
        assert Settings(allow_sd=True, min_height=720).effective_min_height == 0
        assert Settings(allow_sd=False, min_height=720).effective_min_height == 720

    def test_api_key_is_not_exposed_by_repr(self) -> None:
        """A traceback or log line must not print the key."""
        # Assembled at runtime so no credential-shaped literal sits in the source.
        fake = "rad" + "_test_" + "value_not_a_real_key"
        settings = Settings(api_key=fake)
        assert fake not in repr(settings)
        assert settings.require_api_key() == fake

    def test_missing_api_key_has_an_actionable_hint(self) -> None:
        with pytest.raises(ConfigError) as excinfo:
            Settings(api_key=None).require_api_key()
        assert "RAD_API_KEY" in (excinfo.value.hint or "")

    def test_api_url_trailing_slash_is_stripped(self) -> None:
        assert Settings(api_url="https://api.rad.live/").api_url == "https://api.rad.live"
