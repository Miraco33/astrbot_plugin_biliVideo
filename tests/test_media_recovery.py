"""Hermetic media recovery contracts; no platform accounts or external APIs."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import yt_dlp

from bilivideo.core.config import PluginConfig
from bilivideo.core.exceptions import BiliVideoError, DownloadError, TranscriptionError
from bilivideo.core.types import AudioDownloadResult, TranscriptResult, TranscriptSegment
from bilivideo.downloader import ytdlp_downloader as module
from bilivideo.downloader.ytdlp_downloader import (
    DownloadBudgetError,
    DownloadCancelledError,
    YtDlpDownloader,
    _audio_selector,
)
from bilivideo.transcription.astrbot_provider import AstrBotSTTFallback
from bilivideo.transcription.pipeline import TranscriptPipeline


def transcript(text="synthetic speech"):
    return TranscriptResult(
        language="en", full_text=text,
        segments=(TranscriptSegment(start=0, end=1, text=text),),
    )


def audio_file(tmp_path):
    path = tmp_path / "job_fixture" / "sample.mp3"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b"synthetic-audio")
    return AudioDownloadResult(
        file_path=str(path), title="fixture", duration=1,
        cover_url=None, platform="bilibili", video_id="sample", raw_info={},
    )


def test_source_tiers_select_real_source_formats():
    formats = [
        {"format_id": str(rate), "vcodec": "none", "acodec": "aac", "abr": rate}
        for rate in (64, 128, 192)
    ]
    assert [next(_audio_selector(q)({"formats": formats}))["abr"]
            for q in ("fast", "medium", "slow")] == [64, 128, 192]


def test_download_refreshes_source_once_and_continues_partial(tmp_path, monkeypatch):
    instances = []
    seen_part = []

    class FakeDL:
        def __init__(self, opts):
            self.opts = opts
            instances.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=True):
            root = Path(self.opts["outtmpl"]).parent
            part = root / "sample.m4a.part"
            if len(instances) == 1:
                part.write_bytes(b"partial")
                raise yt_dlp.utils.DownloadError("connection closed with bytes read missing")
            seen_part.append(part.read_bytes())
            (root / "sample.mp3").write_bytes(b"complete")
            return {"id": "sample", "title": "fixture", "duration": 1}

    monkeypatch.setattr(module.yt_dlp, "YoutubeDL", FakeDL)
    monkeypatch.setattr(module, "_ffmpeg_location", lambda: None)
    monkeypatch.setattr(module.time, "sleep", lambda _delay: None)
    result = YtDlpDownloader(tmp_path).download_audio("https://example.com/video")
    assert len(instances) == 2
    assert seen_part == [b"partial"]
    assert instances[0].opts["continuedl"] is True
    assert instances[0].opts["retries"] == 2
    assert instances[0].opts["fragment_retries"] == 2
    assert Path(result.file_path).read_bytes() == b"complete"
    TranscriptPipeline.cleanup_audio(result)
    assert not Path(result.file_path).parent.exists()


def test_download_412_is_not_retried_and_never_exposes_raw_error(tmp_path, monkeypatch):
    calls = []

    class FakeDL:
        def __init__(self, opts):
            calls.append(opts)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=True):
            raise yt_dlp.utils.DownloadError("HTTP Error 412 raw-private-diagnostic")

    monkeypatch.setattr(module.yt_dlp, "YoutubeDL", FakeDL)
    monkeypatch.setattr(module, "_ffmpeg_location", lambda: None)
    with pytest.raises(DownloadError) as caught:
        YtDlpDownloader(tmp_path).download_audio("https://example.com/video")
    assert len(calls) == 1
    assert "raw-private" not in str(caught.value)
    assert list(tmp_path.glob("job_*")) == []


def test_pre_cancel_and_duration_budget_cleanup(tmp_path, monkeypatch):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(DownloadCancelledError):
        YtDlpDownloader(tmp_path).download_audio("https://example.com/video", cancel_event=cancel)
    assert list(tmp_path.glob("job_*")) == []

    class FakeDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=True):
            self.opts["match_filter"]({"duration": 61}, incomplete=False)

    monkeypatch.setattr(module.yt_dlp, "YoutubeDL", FakeDL)
    monkeypatch.setattr(module, "_ffmpeg_location", lambda: None)
    with pytest.raises(DownloadBudgetError):
        YtDlpDownloader(tmp_path).download_audio(
            "https://example.com/video", max_duration_minutes=1
        )
    assert list(tmp_path.glob("job_*")) == []


async def test_primary_failure_cleans_audio_without_implicit_fallback(tmp_path):
    audio = audio_file(tmp_path)

    def fail(*args):
        raise TranscriptionError("primary unavailable")

    pipeline = TranscriptPipeline(
        SimpleNamespace(download_audio=lambda *_args, **_kwargs: audio),
        SimpleNamespace(transcribe=fail),
    )
    with pytest.raises(TranscriptionError):
        await pipeline.fetch("https://example.com/video", prefer_subtitle=False)
    assert not Path(audio.file_path).parent.exists()


async def test_explicit_fallback_uses_remaining_budget_and_cleans_audio(tmp_path):
    audio = audio_file(tmp_path)
    calls = []

    def fail(*args):
        raise TranscriptionError("primary unavailable")

    class Fallback:
        async def transcribe(self, path, *, duration):
            calls.append((path, duration))
            return transcript()

    pipeline = TranscriptPipeline(
        SimpleNamespace(download_audio=lambda *_args, **_kwargs: audio),
        SimpleNamespace(transcribe=fail), fallback=Fallback(),
    )
    result = await pipeline.fetch("https://example.com/video", prefer_subtitle=False)
    assert result.transcript.has_content
    assert calls == [(audio.file_path, 1)]
    assert not Path(audio.file_path).parent.exists()


async def test_late_download_result_is_cleaned_after_timeout(tmp_path):
    audio = audio_file(tmp_path)
    release = threading.Event()
    entered = threading.Event()
    observed = []

    def download(*args, **kwargs):
        entered.set()
        release.wait(2)
        observed.append(kwargs["cancel_event"].is_set())
        return audio

    pipeline = TranscriptPipeline(
        SimpleNamespace(download_audio=download),
        SimpleNamespace(transcribe=lambda *_args: transcript()),
        download_timeout_seconds=0.02,
    )
    with pytest.raises(BiliVideoError):
        await pipeline.fetch("https://example.com/video", prefer_subtitle=False)
    assert entered.is_set()
    release.set()
    for _attempt in range(100):
        if not Path(audio.file_path).exists():
            break
        await asyncio.sleep(0.01)
    assert observed == [True]
    assert not Path(audio.file_path).exists()


async def test_fallback_requires_selected_stt_provider_and_preserves_plain_text_contract(tmp_path):
    audio = audio_file(tmp_path)
    calls = []

    class Provider:
        async def get_text(self, path):
            calls.append(path)
            return "synthetic speech"

    selected = Provider()
    context = SimpleNamespace(
        get_all_stt_providers=lambda: [selected],
        get_provider_by_id=lambda provider_id: selected if provider_id == "selected" else None,
    )
    result = await AstrBotSTTFallback(context, "selected").transcribe(audio.file_path, duration=12)
    assert calls == [audio.file_path]
    assert result.raw["timestamps"] is False
    assert result.segments[0].start == 0 and result.segments[0].end == 12
    with pytest.raises(TranscriptionError, match="missing"):
        await AstrBotSTTFallback(context, "missing").transcribe(audio.file_path, duration=12)


def test_media_budgets_are_validated_and_fallback_is_opt_in():
    config = PluginConfig.from_mapping({"summary": {
        "download_timeout_seconds": -1, "asr_timeout_seconds": 9999,
        "max_audio_duration_minutes": 0, "max_audio_size_mb": 9999,
    }})
    assert config.download_timeout_seconds == 30
    assert config.asr_timeout_seconds == 1800
    assert config.max_audio_duration_minutes == 1
    assert config.max_audio_size_mb == 512
    assert config.fallback_asr_provider_id == ""


def test_subtitle_jobs_never_share_or_sweep_other_requests(tmp_path, monkeypatch):
    roots = []
    nested = []
    downloader = YtDlpDownloader(tmp_path)
    video_url = "https://www.bilibili.com/video/BV1xx411c7mD"

    class FakeDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=True):
            root = Path(self.opts["outtmpl"]).parent
            roots.append(root)
            subtitle = root / "BV1xx411c7mD.en.srt"
            subtitle.write_text(
                "1\\n00:00:00,000 --> 00:00:01,000\\nsynthetic speech\\n\\n",
                encoding="utf-8",
            )
            if len(roots) == 1:
                nested.append(downloader.download_subtitles(video_url, langs=("en",)))
                assert subtitle.exists()
            return {"requested_subtitles": {"en": {"ext": "srt"}}}

    monkeypatch.setattr(module.yt_dlp, "YoutubeDL", FakeDL)
    outer = downloader.download_subtitles(video_url, langs=("en",))
    assert outer.has_content and nested[0].has_content
    assert roots[0] != roots[1]
    assert not any(root.exists() for root in roots)


def test_subtitle_extract_failure_cleans_all_job_files(tmp_path, monkeypatch):
    class FakeDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def extract_info(self, url, download=True):
            (Path(self.opts["outtmpl"]).parent / "partial.srt").write_text("partial", encoding="utf-8")
            raise yt_dlp.utils.DownloadError("synthetic network failure")

    monkeypatch.setattr(module.yt_dlp, "YoutubeDL", FakeDL)
    result = YtDlpDownloader(tmp_path).download_subtitles("https://example.com/video")
    assert result is None
    assert list(tmp_path.glob("job_*")) == []
