"""Audio downloader & subtitle harvester via yt-dlp.

Refactored from the previous `BilibiliDownloader`:
  * subtitle parsing extracted into helpers so it can be reused/tested
  * cookie file lifecycle isolated; we always overwrite to keep it in sync
  * uses dataclasses from `core.types`
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import time
from collections.abc import Iterable, Mapping
from functools import lru_cache
from hashlib import sha256
from pathlib import Path

import yt_dlp

from ..core.constants import QUALITY_TO_KBPS, YTDLP_SOCKET_TIMEOUT_SECONDS
from ..core.exceptions import DownloadError, TranscriptionError
from ..core.logging import get_logger
from ..core.types import AudioDownloadResult, TranscriptResult, TranscriptSegment
from ..parsing.url_extractor import detect_platform

logger = get_logger("BiliVideo/Download")


@lru_cache(maxsize=1)
def _ffmpeg_location() -> str | None:
    """Resolve an ffmpeg binary for yt-dlp's audio postprocessor.

    Prefer a system ffmpeg (respect the host/container setup); when none is
    on PATH, fall back to the static binary shipped by ``imageio-ffmpeg`` so
    audio extraction works on a bare VPS with no ``apt install ffmpeg``.
    Returns ``None`` to let yt-dlp search PATH itself.
    """

    if shutil.which("ffmpeg") or shutil.which("ffmpeg.exe"):
        return None
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # pragma: no cover - optional wheel
        logger.warning(f"imageio-ffmpeg unavailable; relying on PATH ffmpeg: {exc}")
        return None


DEFAULT_SUBTITLE_LANGS: tuple[str, ...] = (
    "zh-Hans",
    "zh",
    "zh-CN",
    "ai-zh",
    "en",
    "en-US",
)


class DownloadCancelledError(Exception):
    """Raised from the progress hook when the caller cancelled the download."""


class DownloadBudgetError(DownloadError):
    """A configured duration, byte or time budget was exceeded."""


class _QuietYtDlpLogger:
    """yt-dlp diagnostics can contain signed URLs; log categories only."""

    def debug(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        logger.warning("yt-dlp stage failed; raw diagnostic omitted")


def _audio_selector(quality: str):
    """Select a real source audio tier; available platform tiers may coincide."""

    def select(context: dict):
        formats = context.get("formats") or []
        candidates = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") != "none"]
        if not candidates:
            candidates = [f for f in formats if f.get("acodec") != "none"]
        if not candidates:
            return
        ordered = sorted(candidates, key=lambda f: float(f.get("abr") or f.get("tbr") or 0))
        index = 0 if quality == "fast" else len(ordered) // 2
        if quality == "slow":
            index = len(ordered) - 1
        yield ordered[index]

    return select


class YtDlpDownloader:
    """Bilibili audio + subtitle downloader."""

    def __init__(
        self,
        data_dir: str | Path,
        *,
        cookies: Mapping[str, str] | None = None,
        youtube_cookies_file: str | Path | None = None,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._cookies_file: Path | None = None
        self._youtube_cookies_file: Path | None = (
            Path(youtube_cookies_file) if youtube_cookies_file else None
        )
        self.update_cookies(cookies)

    # ------------------------------------------------------------------
    # cookie helpers
    # ------------------------------------------------------------------
    def update_cookies(self, cookies: Mapping[str, str] | None) -> None:
        if not cookies:
            path = self._data_dir / "cookies.txt"
            if path.exists():
                try:
                    path.unlink()
                    logger.debug(f"downloader_cookiefile_removed path={path}")
                except OSError as exc:
                    logger.warning(f"cookies.txt removal failed: {exc}")
            self._cookies_file = None
            return
        path = self._data_dir / "cookies.txt"
        lines = ["# Netscape HTTP Cookie File"]
        for name, value in cookies.items():
            if value:
                lines.append(f".bilibili.com\tTRUE\t/\tFALSE\t0\t{name}\t{value}")
        try:
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            os.chmod(path, 0o600)
            logger.debug(f"downloader_cookiefile_written path={path} keys={list(cookies.keys())}")
        except OSError as exc:
            logger.warning(f"cookies.txt write failed: {exc}")
            self._cookies_file = None
            return
        self._cookies_file = path

    def _cookiefile_for(self, video_url: str) -> str | None:
        """Pick the cookie jar for the target platform.

        YouTube increasingly blocks datacenter IPs with a "confirm you're not
        a bot" check; an admin-supplied cookies.txt (exported from a burner
        Google account) is the documented workaround. Bilibili keeps using the
        login cookies written by ``update_cookies``.
        """

        if detect_platform(video_url) == "youtube":
            if self._youtube_cookies_file and self._youtube_cookies_file.exists():
                return str(self._youtube_cookies_file)
            return None
        if self._cookies_file and self._cookies_file.exists():
            return str(self._cookies_file)
        return None

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def download_audio(
        self,
        video_url: str,
        *,
        output_dir: str | Path | None = None,
        quality: str = "fast",
        cancel_event: threading.Event | None = None,
        timeout_seconds: float = 150,
        max_duration_minutes: int = 180,
        max_size_mb: int = 128,
    ) -> AudioDownloadResult:
        target = Path(output_dir) if output_dir else self._data_dir
        target.mkdir(parents=True, exist_ok=True)
        # Isolate concurrent requests and cleanup; never sweep another job's
        # partial file, even if both requests reference the same video.
        job_dir = Path(tempfile.mkdtemp(prefix="job_", dir=target))
        deadline = time.monotonic() + timeout_seconds
        max_bytes = max_size_mb * 1024 * 1024
        success = False

        def check(progress: dict | None = None) -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise DownloadCancelledError("download cancelled by caller")
            if time.monotonic() >= deadline:
                raise DownloadBudgetError(
                    "download time budget exceeded",
                    user_message="❌ 音频下载超时，可提高下载预算后重试",
                )
            if progress and int(progress.get("downloaded_bytes") or 0) > max_bytes:
                raise DownloadBudgetError(
                    "download byte budget exceeded",
                    user_message="❌ 源音频超过大小预算，请选择更低音轨或提高大小限制",
                )

        def match_filter(info: dict, *, incomplete: bool = False) -> str | None:
            check()
            duration = float(info.get("duration") or 0)
            if duration > max_duration_minutes * 60:
                raise DownloadBudgetError(
                    "audio duration budget exceeded",
                    user_message="❌ 视频超过音频处理时长限制，请提高预算或使用平台字幕",
                )
            return None

        opts = {
            "format": _audio_selector(quality),
            "outtmpl": str(job_dir / "%(id)s.%(ext)s"),
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": QUALITY_TO_KBPS.get(quality, "64"),
            }],
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "logger": _QuietYtDlpLogger(),
            "socket_timeout": min(YTDLP_SOCKET_TIMEOUT_SECONDS, timeout_seconds),
            "retries": 2,
            "fragment_retries": 2,
            "extractor_retries": 2,
            "file_access_retries": 1,
            "continuedl": True,
            "nopart": False,
            "max_filesize": max_bytes,
            "match_filter": match_filter,
            "progress_hooks": [check],
            "postprocessor_hooks": [check],
        }
        cookiefile = self._cookiefile_for(video_url)
        if cookiefile:
            opts["cookiefile"] = cookiefile
        ffmpeg_path = _ffmpeg_location()
        if ffmpeg_path:
            opts["ffmpeg_location"] = ffmpeg_path

        try:
            # A fresh extract_info call obtains fresh media URLs after one
            # transient failure; .part files remain available for continuation.
            for attempt in range(2):
                check()
                try:
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(video_url, download=True)
                    check()
                    if not isinstance(info, dict):
                        raise DownloadError("audio metadata missing")
                    video_id = str(info.get("id") or "")
                    audio_path = job_dir / f"{video_id}.mp3"
                    if audio_path.parent != job_dir or not audio_path.is_file():
                        raise DownloadError(
                            "audio output missing or source size budget exceeded",
                            user_message="❌ 音频未生成，可能超过大小预算或转码失败",
                        )
                    if not 0 < audio_path.stat().st_size <= max_bytes:
                        raise DownloadBudgetError(
                            "audio output byte budget exceeded",
                            user_message="❌ 音频为空或超过处理大小限制",
                        )
                    success = True
                    return AudioDownloadResult(
                        file_path=str(audio_path),
                        title=str(info.get("title", "")),
                        duration=float(info.get("duration", 0) or 0),
                        cover_url=info.get("thumbnail"),
                        platform=str(info.get("extractor_key") or info.get("extractor") or "bilibili").lower(),
                        video_id=video_id,
                        raw_info=dict(info),
                    )
                except yt_dlp.utils.DownloadError as exc:
                    check()
                    if attempt or not _retryable_download_error(exc):
                        raise _wrap_download_error(exc, video_url) from None
                    logger.warning("audio download interrupted; refreshing media source once")
                    if cancel_event is not None:
                        if cancel_event.wait(0.6):
                            raise DownloadCancelledError("download cancelled by caller") from None
                    else:
                        time.sleep(0.6)
            raise DownloadError("audio retry budget exhausted")
        finally:
            if not success:
                shutil.rmtree(job_dir, ignore_errors=True)

    @staticmethod
    def _sweep_partial_files(target: Path, video_url: str) -> None:
        """Best-effort removal of output a cancelled download left behind."""

        video_id = _extract_video_id_from_url(video_url)
        if not video_id:
            return
        for path in target.glob(f"{video_id}.*"):
            try:
                path.unlink()
            except OSError as exc:
                logger.debug(f"partial file cleanup failed for {path}: {exc}")

    def download_subtitles(
        self,
        video_url: str,
        *,
        output_dir: str | Path | None = None,
        langs: Iterable[str] | None = None,
    ) -> TranscriptResult | None:
        target = Path(output_dir) if output_dir else self._data_dir
        target.mkdir(parents=True, exist_ok=True)
        lang_list = list(langs) if langs else list(DEFAULT_SUBTITLE_LANGS)
        video_id = _extract_video_id_from_url(video_url) or _stable_video_id(video_url)

        opts = {
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": lang_list,
            "subtitlesformat": "srt/json3/best",
            "skip_download": True,
            "outtmpl": str(target / f"{video_id}.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": YTDLP_SOCKET_TIMEOUT_SECONDS,
            "logger": _QuietYtDlpLogger(),
            "retries": 2,
            "extractor_retries": 2,
        }
        cookiefile = self._cookiefile_for(video_url)
        if cookiefile:
            opts["cookiefile"] = cookiefile

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(video_url, download=True)
        except yt_dlp.utils.DownloadError as exc:
            logger.warning(f"subtitle download failed category={type(exc).__name__}")
            return None

        subtitles = info.get("requested_subtitles") or {}
        if not subtitles:
            logger.info(f"{video_id}: no platform subtitles")
            return None

        # Subtitle files ({video_id}.{lang}.{ext}) are only needed for this
        # parse; sweep them afterwards so they don't accumulate forever. The
        # audio file is a single-suffix "{video_id}.mp3" and is not matched.
        try:
            for lang in lang_list:
                if lang in subtitles:
                    return _parse_sub(subtitles[lang], lang, target, video_id)
            # any other available language except 'danmaku'
            for lang, sub_info in subtitles.items():
                if lang == "danmaku":
                    continue
                return _parse_sub(sub_info, lang, target, video_id)
            return None
        finally:
            for path in target.glob(f"{video_id}.*.*"):
                try:
                    path.unlink()
                except OSError as exc:
                    logger.debug(f"subtitle cleanup failed for {path}: {exc}")


# ──────────────────────────── helpers ──────────────────────────────


_YT_BOT_CHECK_SIGNS: tuple[str, ...] = (
    "sign in to confirm",
    "confirm you're not a bot",
    "confirm you are not a bot",
)


def _retryable_download_error(exc: Exception) -> bool:
    """Refresh only transport/expired-media failures; never loop on 412."""
    text = str(exc).lower()
    if any(sign in text for sign in ("412", "429", "not available", "video unavailable", "copyright")):
        return False
    return any(sign in text for sign in (
        "timed out", "timeout", "connection", "bytes read", "incomplete",
        "remote end closed", "http error 403", "http error 410", "http error 5",
    ))


def _wrap_download_error(exc: Exception, video_url: str) -> DownloadError:
    """Map a yt-dlp download failure to a DownloadError with a useful hint.

    YouTube on a datacenter IP often refuses with a "confirm you're not a bot"
    sign-in wall. Surface a message that points at /YT登录 and the burner-account
    workaround instead of the generic copyright/deleted hint.
    """

    message = str(exc)
    safe_message = f"yt-dlp download failed category={type(exc).__name__}"
    if detect_platform(video_url) == "youtube" and any(
        sign in message.lower() for sign in _YT_BOT_CHECK_SIGNS
    ):
        return DownloadError(
            safe_message,
            user_message=(
                "❌ YouTube 拒绝下载:需要登录验证(VPS 机房 IP 常被要求\"确认你不是机器人\")。\n"
                "请发送 /YT登录 查看如何提供 cookies。\n"
                "⚠️ 强烈建议使用小号/不重要的 Google 账号,有封号风险!"
            ),
        )
    return DownloadError(safe_message)


def _extract_video_id_from_url(url: str) -> str | None:
    match = re.search(r"BV[0-9A-Za-z]{10}", url or "")
    return match.group(0) if match else None


def _stable_video_id(url: str) -> str:
    return "video_" + sha256((url or "").encode("utf-8")).hexdigest()[:12]


def _parse_sub(sub_info: dict, language: str, output_dir: Path, video_id: str) -> TranscriptResult | None:
    inline = sub_info.get("data")
    if isinstance(inline, str) and inline:
        return _parse_srt(inline, language)
    ext = sub_info.get("ext", "srt")
    path = output_dir / f"{video_id}.{language}.{ext}"
    if not path.exists():
        return None
    if ext == "json3":
        return _parse_json3(path, language)
    try:
        return _parse_srt(path.read_text(encoding="utf-8"), language)
    except OSError as exc:
        raise TranscriptionError(f"subtitle file read error: {exc}") from exc


def _parse_srt(content: str, language: str) -> TranscriptResult | None:
    pattern = re.compile(
        r"(\d+)\n(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})\n(.*?)(?=\n\n|\n\d+\n|$)",
        re.DOTALL,
    )
    segments: list[TranscriptSegment] = []
    for _, start_t, end_t, text in pattern.findall(content):
        text = text.strip()
        if not text:
            continue
        segments.append(
            TranscriptSegment(
                start=_srt_time(start_t),
                end=_srt_time(end_t),
                text=text,
            )
        )
    if not segments:
        return None
    full_text = " ".join(seg.text for seg in segments)
    return TranscriptResult(
        language=language,
        full_text=full_text,
        segments=tuple(segments),
        raw={"source": "bilibili_subtitle", "format": "srt"},
    )


def _parse_json3(path: Path, language: str) -> TranscriptResult | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"json3 subtitle parse failed: {exc}")
        return None

    segments: list[TranscriptSegment] = []
    for event in data.get("events", []):
        start_ms = event.get("tStartMs", 0)
        duration_ms = event.get("dDurationMs", 0)
        text = "".join(s.get("utf8", "") for s in event.get("segs", [])).strip()
        if not text:
            continue
        segments.append(
            TranscriptSegment(
                start=start_ms / 1000.0,
                end=(start_ms + duration_ms) / 1000.0,
                text=text,
            )
        )
    if not segments:
        return None
    return TranscriptResult(
        language=language,
        full_text=" ".join(seg.text for seg in segments),
        segments=tuple(segments),
        raw={"source": "bilibili_subtitle", "format": "json3"},
    )


def _srt_time(token: str) -> float:
    parts = token.replace(",", ".").split(":")
    return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
