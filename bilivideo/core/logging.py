"""Tagged, privacy-aware runtime logging.

Render logging arguments before redaction so deferred formatting cannot bypass
it. Exceptions are reduced to class and HTTP status; raw tracebacks, stack dumps
and logger extras never reach the host logger. This is defense in depth, not a
substitute for using exception_summary at call sites and reviewing publications.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any
from urllib.parse import urlsplit

try:
    from astrbot.api import logger as _astrbot_logger  # type: ignore[import]
except Exception:  # pragma: no cover - test environment
    _astrbot_logger = None  # type: ignore[assignment]


_FALLBACK = logging.getLogger("bilivideo")
_URL = re.compile(r"""https?://[^\s"'<>]+""", re.IGNORECASE)
_UMO = re.compile(
    r"""(?<![\w])[\w.-]+:[A-Za-z]+Message:[^\s,;"'<>]+""",
    re.IGNORECASE,
)
# These contexts identify accounts/targets without hiding byte counts, durations
# or version components simply because they contain several digits.
_TARGET = re.compile(
    r"""(?ix)
    (?P<key>\b(?:target(?:_id)?|session(?:_id)?|user_?id|group_?id|uid|mid|qq|dedeuserid)
    ["']?\s*[:=]\s*["']?)
    [^,\s;"'}\]]+
    """
)
_PUSH_TARGET = re.compile(r"(?i)(\bpush\s+to\s+)[^\s:;,]+")
_LOOKUP_ID = re.compile(r"(\b(?:get_uploader_info|get_latest_videos)\()\d+(\))")
_WINDOWS_PATH = re.compile(r"""(?<!\w)(?:[A-Za-z]:[\\/]|\\\\)[^\s"'<>|,;]+""")
_UNIX_PATH = re.compile(r"""(?<![\w:])/(?:[^/\s"'<>|,;]+/)*[^/\s"'<>|,;]+""")
_SECRET_KEY = (
    r"(?:authorization|proxy-authorization|cookie|set-cookie|"
    r"(?:[a-z0-9]+[_-])*api[_-]?key|(?:[a-z0-9]+[_-])*"
    r"(?:access[_-]?token|refresh[_-]?token|client[_-]?secret|password|passwd|token)|"
    r"SESSDATA|bili_jct|csrf|qrcode_key|w_rid|wssecret|auth_key|signature|sign|sig)"
)
_QUOTED_SECRET = re.compile(
    rf"""(?ix)(?P<key>(?<![\w-]){_SECRET_KEY}["']?\s*[:=]\s*)
    (?P<quote>["'])(?P<value>.*?)(?P=quote)"""
)
_UNQUOTED_SECRET = re.compile(
    rf"""(?ix)(?P<key>(?<![\w-]){_SECRET_KEY}["']?\s*[:=]\s*)
    (?!["'<])(?P<value>[^\r\n,}}\]]+)"""
)
_BEARER = re.compile(r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+")
_TOKEN = re.compile(
    r"(?<![\w-])(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"sk-(?:proj-|org-)?[A-Za-z0-9_-]{16,}|xox[baprs]-[A-Za-z0-9-]{16,})(?![\w-])"
)
_HTTP_STATUS = re.compile(r"(?i)\b(?:HTTP(?:/\d(?:\.\d)?)?\s*|http_status[=:]\s*)([1-5]\d{2})\b")
_TRACE_CLASS = re.compile(r"(?m)^([A-Za-z_][\w.]*(?:Error|Exception|Failure|Timeout)):")
_SAFE_HOSTS = ("bilibili.com", "bilivideo.com", "b23.tv", "bcut.site", "youtube.com", "youtu.be")


def exception_summary(exc: BaseException | None) -> str:
    """Return only an exception class and verified numeric HTTP status."""
    if exc is None:
        return "exception"
    name = type(exc).__name__
    name = re.sub(r"[^A-Za-z0-9_]", "", name)[:80] or "exception"
    # Do not call str/repr on exceptions or responses: either can carry a signed
    # URL, credentials, server response body, or a personal filesystem path.
    status = getattr(exc, "status", None)
    if status is None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "http_status", None)
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        return f"{name} http_status={status}"
    return name


def _safe_url(match: re.Match[str]) -> str:
    """Retain only known public service hosts; drop every path/query/fragment."""
    try:
        parsed = urlsplit(match[0].rstrip(").,;"))
        host = (parsed.hostname or "").lower()
    except ValueError:
        return "<url>"
    if any(host == suffix or host.endswith("." + suffix) for suffix in _SAFE_HOSTS):
        return f"<url host={host}>"
    return "<url>"


def sanitize_log(value: object) -> str:
    """Redact common runtime identifiers without changing diagnostic numbers."""
    text = str(value)
    if "Traceback (most recent call last):" in text:
        prefix, _, trace = text.partition("Traceback (most recent call last):")
        classes = _TRACE_CLASS.findall(trace)
        statuses = _HTTP_STATUS.findall(trace)
        suffix = f" exception={classes[-1]}" if classes else ""
        if statuses:
            suffix += f" http_status={statuses[-1]}"
        text = prefix + "<traceback omitted>" + suffix
    text = _URL.sub(_safe_url, text)
    text = _QUOTED_SECRET.sub(lambda m: f"{m['key']}'<redacted>'", text)
    text = _UNQUOTED_SECRET.sub(lambda m: f"{m['key']}<redacted>", text)
    text = _BEARER.sub("<authentication>", text)
    text = _TOKEN.sub("<credential>", text)
    text = _UMO.sub("<session>", text)
    text = _TARGET.sub(lambda m: f"{m['key']}<target>", text)
    text = _PUSH_TARGET.sub(r"\1<target>", text)
    text = _LOOKUP_ID.sub(r"\1<account>\2", text)
    # URLs have already been replaced. Keep generated redaction markers intact.
    text = _WINDOWS_PATH.sub("<path>", text)
    text = _UNIX_PATH.sub("<path>", text)
    return text


def _safe_argument(value: Any) -> Any:
    if isinstance(value, BaseException):
        return exception_summary(value)
    if isinstance(value, dict):
        return {str(key): _safe_argument(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return tuple(_safe_argument(item) for item in value)
    return value


def _format_message(msg: object, args: tuple[Any, ...]) -> str:
    message = exception_summary(msg) if isinstance(msg, BaseException) else str(msg)
    if not args:
        return message
    safe_args = tuple(_safe_argument(item) for item in args)
    try:
        # Match stdlib logging's single-mapping convention.
        if len(safe_args) == 1 and isinstance(safe_args[0], dict):
            return message % safe_args[0]
        return message % safe_args
    except (TypeError, ValueError, KeyError):
        # Formatting errors must not drop the diagnostic or emit raw arguments.
        return f"{message} [log formatting failed]"


class TaggedLogger:
    """Prefix and sanitize each log record before handing it to AstrBot."""

    __slots__ = ("_debug_enabled", "_tag")

    def __init__(self, tag: str, *, debug_enabled: bool = False) -> None:
        self._tag = tag
        self._debug_enabled = debug_enabled

    def set_debug(self, enabled: bool) -> None:
        self._debug_enabled = enabled

    def _emit(self, level: str, msg: str, *args: Any, **kwargs: Any) -> None:
        text = _format_message(msg, args)
        exc_info = kwargs.pop("exc_info", None)
        exc = None
        if isinstance(exc_info, BaseException):
            exc = exc_info
        elif isinstance(exc_info, tuple) and len(exc_info) == 3:
            exc = exc_info[1]
        else:
            # sys.exc_info remains available in helper calls made from an
            # except block. This also protects legacy f-string call sites
            # that did not explicitly request a traceback.
            exc = sys.exc_info()[1]
        if isinstance(exc, BaseException):
            # Existing f-string call sites may already contain str(exc).
            # Replace it before generic redaction, then omit the raw traceback.
            raw = str(exc)
            if raw:
                text = text.replace(raw, exception_summary(exc))
            if exc_info:
                text += f" [exception={exception_summary(exc)}]"
        elif exc_info:
            text += " [traceback omitted]"
        # Host formatters may render extras or stack_info after our message was
        # sanitized. Pass only stacklevel, whose value never carries user data.
        options = {}
        stacklevel = kwargs.get("stacklevel")
        if isinstance(stacklevel, int) and not isinstance(stacklevel, bool):
            options["stacklevel"] = stacklevel
        line = sanitize_log(f"[{self._tag}] {text}")
        target = _astrbot_logger or _FALLBACK
        method = getattr(target, level, None)
        if method is None:
            method = target.info  # type: ignore[union-attr]
        method(line, **options)

    def info(self, msg: str, *a: Any, **kw: Any) -> None:
        self._emit("info", msg, *a, **kw)

    def warning(self, msg: str, *a: Any, **kw: Any) -> None:
        self._emit("warning", msg, *a, **kw)

    def error(self, msg: str, *a: Any, **kw: Any) -> None:
        self._emit("error", msg, *a, **kw)

    def debug(self, msg: str, *a: Any, **kw: Any) -> None:
        """Show sanitized debug messages at INFO only when explicitly enabled."""
        if self._debug_enabled:
            self._emit("info", f"DBG {msg}", *a, **kw)


def get_logger(tag: str, *, debug_enabled: bool = False) -> TaggedLogger:
    return TaggedLogger(tag, debug_enabled=debug_enabled)
