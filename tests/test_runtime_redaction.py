"""Synthetic privacy regression cases; never call a real account or API."""

from __future__ import annotations

from unittest.mock import patch

from bilivideo.core import logging as runtime_logging
from bilivideo.core.logging import TaggedLogger, exception_summary, sanitize_log


class _Sink:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def info(self, message: str, **kwargs) -> None:
        self.records.append((message, kwargs))

    warning = info
    error = info


def test_session_and_contextual_target_are_redacted() -> None:
    session = "test-instance:GroupMessage:" + str(123456789)
    output = sanitize_log(f"push to {session} failed target=123456789 uid=456789")
    assert "test-instance" not in output
    assert "123456789" not in output
    assert "456789" not in output
    assert "failed" in output


def test_diagnostic_numbers_are_preserved() -> None:
    message = "version=2.1.1 HTTP 412 bytes=655360 elapsed=150s poll=1/50 code=-400"
    assert sanitize_log(message) == message


def test_media_urls_lose_path_query_fragment_and_userinfo() -> None:
    output = sanitize_log(
        "stage=download https://" + "test_user:test_password@cdn.bilivideo.com/"
        "test_private_path?signature=test_signature#test_fragment"
    )
    assert "stage=download" in output
    assert "host=cdn.bilivideo.com" in output
    for value in ("test_user", "test_password", "test_private_path", "test_signature", "test_fragment"):
        assert value not in output


def test_personal_service_host_and_private_address_are_not_logged() -> None:
    output = sanitize_log("https://test-personal.invalid/path http://127.0.0.1:8000/private")
    assert "test-personal" not in output
    assert "127.0.0.1" not in output


def test_paths_are_redacted_without_touching_poll_fraction() -> None:
    output = sanitize_log(
        r"stage=render C:\Users\test_person\private\image.png "
        "/home/test_person/private/output.png poll=1/50"
    )
    assert "test_person" not in output
    assert "image.png" not in output
    assert "poll=1/50" in output


def test_authentication_fields_are_redacted() -> None:
    for message in (
        "stage=upload Authorization: Bearer test_header_value",
        "stage=upload Cookie: SESSDATA=test_cookie_value; bili_jct=test_csrf",
        "{'X-API-Key': 'test_header_value', 'stage': 'upload'}",
        'stage=upload api_key="test_header_value"',
        "stage=upload password=test_password_value",
        "stage=upload signature=test_signature_value",
    ):
        output = sanitize_log(message)
        assert "upload" in output
        for value in ("test_header_value", "test_cookie_value", "test_csrf",
                      "test_password_value", "test_signature_value"):
            assert value not in output


def test_deferred_formatting_cannot_bypass_redaction() -> None:
    sink = _Sink()
    with patch.object(runtime_logging, "_astrbot_logger", sink):
        TaggedLogger("test").warning("download failed target=%s url=%s", "123456789",
                                    "https://cdn.bilivideo.com/private?token=test_value")
    output, options = sink.records[0]
    assert "123456789" not in output
    assert "test_value" not in output
    assert "download failed" in output
    assert options == {}


def test_mapping_formatting_and_exception_arguments_are_safe() -> None:
    sink = _Sink()
    error = ValueError("test_unlabelled_private_value")
    with patch.object(runtime_logging, "_astrbot_logger", sink):
        logger = TaggedLogger("test")
        logger.warning("stage=%(stage)s target=%(target)s", {"stage": "send", "target": "123456789"})
        logger.error("stage=decode failed: %s", error)
    assert "123456789" not in sink.records[0][0]
    assert "test_unlabelled_private_value" not in sink.records[1][0]
    assert "ValueError" in sink.records[1][0]


def test_exc_info_and_extras_cannot_leak_raw_exception_or_traceback() -> None:
    sink = _Sink()
    with patch.object(runtime_logging, "_astrbot_logger", sink):
        try:
            raise RuntimeError("test_unlabelled_private_value")
        except RuntimeError as exc:
            TaggedLogger("test").error(f"stage=decode failed: {exc}", exc_info=True,
                                      stack_info=True, extra={"private": "test_extra_value"})
    output, options = sink.records[0]
    assert "RuntimeError" in output
    assert "stage=decode" in output
    assert "test_unlabelled_private_value" not in output
    assert "test_extra_value" not in output
    assert "Traceback" not in output
    assert options == {}


def test_exception_tuple_and_http_status_are_retained_safely() -> None:
    class _HTTPError(Exception):
        status = 412

    error = _HTTPError("test_unlabelled_private_value")
    assert exception_summary(error) == "_HTTPError http_status=412"
    sink = _Sink()
    with patch.object(runtime_logging, "_astrbot_logger", sink):
        TaggedLogger("test").warning("stage=poll failed", exc_info=(_HTTPError, error, None))
    assert "http_status=412" in sink.records[0][0]
    assert "test_unlabelled_private_value" not in sink.records[0][0]


def test_embedded_traceback_does_not_emit_exception_body_or_source_lines() -> None:
    raw = (
        "stage=poll\nTraceback (most recent call last):\n"
        '  File "/home/test_person/private/main.py", line 2\n'
        "    api_call(test_unlabelled_private_value)\n"
        "RuntimeError: test_unlabelled_private_value HTTP 412\n"
    )
    output = sanitize_log(raw)
    assert "stage=poll" in output
    assert "RuntimeError" in output
    assert "http_status=412" in output
    assert "test_person" not in output
    assert "test_unlabelled_private_value" not in output


def test_debug_gate_and_format_errors_keep_existing_behavior() -> None:
    sink = _Sink()
    with patch.object(runtime_logging, "_astrbot_logger", sink):
        logger = TaggedLogger("test")
        logger.debug("not emitted")
        assert not sink.records
        logger.set_debug(True)
        logger.debug("phase=%s", "poll")
        logger.warning("malformed=%s %s", "test_argument")
    assert "DBG phase=poll" in sink.records[0][0]
    assert "log formatting failed" in sink.records[1][0]
    assert "test_argument" not in sink.records[1][0]
