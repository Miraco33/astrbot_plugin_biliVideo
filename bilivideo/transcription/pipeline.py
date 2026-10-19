"""Subtitle-first transcript acquisition with bounded, opt-in ASR recovery."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..core.constants import (
    ASR_TIMEOUT_SECONDS,
    AUDIO_DOWNLOAD_TIMEOUT_SECONDS,
    SUBTITLE_FETCH_TIMEOUT_SECONDS,
)
from ..core.exceptions import BiliVideoError, TranscriptionError
from ..core.logging import get_logger
from ..core.types import AudioDownloadResult, TranscriptResult
from ..downloader.ytdlp_downloader import YtDlpDownloader
from .astrbot_provider import AstrBotSTTFallback
from .bcut_provider import BCutTranscriber

logger = get_logger("BiliVideo/Pipeline")


@dataclass(slots=True)
class PipelineOutput:
    transcript: TranscriptResult
    audio: AudioDownloadResult | None


class TranscriptPipeline:
    def __init__(
        self,
        downloader: YtDlpDownloader,
        transcriber: BCutTranscriber,
        *,
        download_timeout_seconds: int = AUDIO_DOWNLOAD_TIMEOUT_SECONDS,
        asr_timeout_seconds: int = ASR_TIMEOUT_SECONDS,
        max_audio_duration_minutes: int = 180,
        max_audio_size_mb: int = 128,
        fallback: AstrBotSTTFallback | None = None,
    ) -> None:
        self._downloader = downloader
        self._transcriber = transcriber
        self._download_timeout = download_timeout_seconds
        self._asr_timeout = asr_timeout_seconds
        self._max_duration = max_audio_duration_minutes
        self._max_size = max_audio_size_mb
        self._fallback = fallback

    async def fetch(
        self,
        video_url: str,
        *,
        prefer_subtitle: bool = True,
        quality: str = "fast",
        subtitle_langs: tuple[str, ...] | None = None,
    ) -> PipelineOutput:
        loop = asyncio.get_running_loop()
        if prefer_subtitle:
            transcript = await self._fetch_subtitles(video_url, subtitle_langs)
            if transcript and transcript.has_content:
                logger.info(f"subtitle hit ({len(transcript.segments)} segments)")
                return PipelineOutput(transcript=transcript, audio=None)
            logger.info("no platform subtitle, fall back to ASR")

        download_cancel = threading.Event()
        download = loop.run_in_executor(
            None,
            lambda: self._downloader.download_audio(
                video_url,
                quality=quality,
                cancel_event=download_cancel,
                timeout_seconds=self._download_timeout,
                max_duration_minutes=self._max_duration,
                max_size_mb=self._max_size,
            ),
        )
        try:
            # Shield lets late worker output be cleaned if cancellation occurs
            # between finishing conversion and returning the result.
            audio = await asyncio.wait_for(asyncio.shield(download), timeout=self._download_timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            download_cancel.set()
            download.add_done_callback(self._cleanup_download_result)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise BiliVideoError(
                "audio download time budget exceeded",
                user_message="❌ 音频下载超时，请稍后重试或提高下载预算",
            ) from None

        worker = None
        cancel_event = threading.Event()
        try:
            # Preserve the legacy audio-first preference: subtitles may still
            # avoid ASR after metadata/audio have been obtained.
            if not prefer_subtitle:
                transcript = await self._fetch_subtitles(video_url, subtitle_langs)
                if transcript and transcript.has_content:
                    return PipelineOutput(transcript=transcript, audio=audio)
            if audio.duration > self._max_duration * 60:
                raise TranscriptionError("audio duration budget exceeded")
            if Path(audio.file_path).stat().st_size > self._max_size * 1024 * 1024:
                raise TranscriptionError("audio byte budget exceeded")
            deadline = time.monotonic() + self._asr_timeout
            # Reserve time for fallback when explicitly enabled; all ASR
            # attempts together remain within the configured ASR stage budget.
            primary_budget = self._asr_timeout * (0.7 if self._fallback else 1)
            worker = loop.run_in_executor(
                None, self._transcriber.transcribe, audio.file_path, cancel_event
            )
            try:
                transcript = await asyncio.wait_for(asyncio.shield(worker), timeout=primary_budget)
                if transcript is None or not transcript.has_content:
                    raise TranscriptionError("primary ASR yielded empty transcript")
            except (TranscriptionError, asyncio.TimeoutError) as exc:
                cancel_event.set()
                if self._fallback is None:
                    if isinstance(exc, asyncio.TimeoutError):
                        raise BiliVideoError(
                            "ASR time budget exceeded",
                            user_message="❌ 语音转写(ASR)超时，可提高转写预算后重试",
                        ) from None
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TranscriptionError("ASR budget exhausted before fallback") from None
                logger.warning(f"primary ASR failed category={type(exc).__name__}; using selected fallback")
                try:
                    transcript = await asyncio.wait_for(
                        self._fallback.transcribe(audio.file_path, duration=audio.duration),
                        timeout=remaining,
                    )
                except asyncio.TimeoutError:
                    raise TranscriptionError("fallback ASR time budget exceeded") from None
            if transcript is None or not transcript.has_content:
                raise TranscriptionError("all enabled transcript sources returned empty content")
            return PipelineOutput(transcript=transcript, audio=audio)
        finally:
            cancel_event.set()
            self.cleanup_audio(audio)
            # A requests/FFmpeg call already in progress cooperates only once
            # it returns. Keep cleanup attached until that worker finishes.
            if worker is not None:
                worker.add_done_callback(lambda done: self._finish_asr(done, audio))

    async def _fetch_subtitles(
        self, video_url: str, subtitle_langs: tuple[str, ...] | None
    ) -> TranscriptResult | None:
        loop = asyncio.get_running_loop()
        subtitle_cancel = threading.Event()
        subtitle_worker = loop.run_in_executor(
            None,
            lambda: self._downloader.download_subtitles(
                video_url, langs=subtitle_langs, cancel_event=subtitle_cancel,
                timeout_seconds=SUBTITLE_FETCH_TIMEOUT_SECONDS,
            ),
        )
        subtitle_worker.add_done_callback(self._consume_worker)
        try:
            transcript = await asyncio.wait_for(
                asyncio.shield(subtitle_worker), timeout=SUBTITLE_FETCH_TIMEOUT_SECONDS
            )
        except (asyncio.TimeoutError, TranscriptionError):
            subtitle_cancel.set()
            logger.warning("subtitle stage failed within budget; no usable transcript")
            transcript = None
        except asyncio.CancelledError:
            subtitle_cancel.set()
            raise
        return transcript

    @staticmethod
    def _consume_worker(future: asyncio.Future) -> None:
        with contextlib.suppress(BaseException):
            future.result()

    @classmethod
    def _cleanup_download_result(cls, future: asyncio.Future) -> None:
        try:
            audio = future.result()
        except BaseException:
            return
        cls.cleanup_audio(audio)

    @classmethod
    def _finish_asr(cls, future: asyncio.Future, audio: AudioDownloadResult) -> None:
        with contextlib.suppress(BaseException):
            future.result()
        cls.cleanup_audio(audio)

    @staticmethod
    def cleanup_audio(audio_meta: AudioDownloadResult | None) -> None:
        if not audio_meta or not audio_meta.file_path:
            return
        path = Path(audio_meta.file_path)
        try:
            path.unlink(missing_ok=True)
            if path.parent.name.startswith("job_"):
                shutil.rmtree(path.parent, ignore_errors=True)
        except OSError as exc:
            logger.warning(f"audio cleanup failed category={type(exc).__name__}")
