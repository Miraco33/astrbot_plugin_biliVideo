"""Hermetic BCut recovery, streaming, validation and disclosure regressions."""

from __future__ import annotations

import json
import threading

import pytest
import requests

from bilivideo.transcription import bcut_provider as bcut
from bilivideo.transcription.bcut_provider import BCutStageError, BCutTranscriber

_FAKE_URL = "https://example.invalid/mock-upload"
_FAKE_PRIVATE = "mock-private-message"


class _Response:
    def __init__(self, payload=None, status=200, etag="mock-etag") -> None:
        self.payload = payload
        self.status_code = status
        self.headers = {"Etag": etag}
        self.closed = False

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def raise_for_status(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _Session:
    def __init__(self, replies) -> None:
        self.replies = list(replies)
        self.calls = []

    def _next(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def get(self, url, **kwargs):
        return self._next("get", url, **kwargs)

    def post(self, url, **kwargs):
        return self._next("post", url, **kwargs)

    def put(self, url, **kwargs):
        return self._next("put", url, **kwargs)


def _result(state=4):
    return _Response({"code": 0, "data": {"state": state, "result": "{}"}})


def _meta(size, per_size):
    return {
        "upload_urls": [_FAKE_URL] * ((size + per_size - 1) // per_size),
        "per_size": per_size,
        "in_boss_key": "mock-resource-key",
        "resource_id": "mock-resource-id",
        "upload_id": "mock-upload-id",
    }


def test_412_has_its_own_safe_error_and_is_not_retried() -> None:
    response = _Response({"message": _FAKE_PRIVATE}, status=412)
    session = _Session([response, _result()])
    with pytest.raises(BCutStageError) as caught:
        BCutTranscriber()._await_result(session, "mock-task")
    error = caught.value
    assert error.stage == "query_result"
    assert error.category == "precondition_failed"
    assert error.http_status == 412
    assert "原因尚未确定" in error.user_message
    assert "未自动重试" in error.user_message
    assert _FAKE_PRIVATE not in str(error)
    assert _FAKE_URL not in str(error)
    assert len(session.calls) == 1
    assert response.closed


@pytest.mark.parametrize("url", [bcut.ENDPOINT_CREATE, bcut.ENDPOINT_COMMIT, bcut.ENDPOINT_TASK])
def test_post_network_failure_is_not_retried_or_disclosed(url) -> None:
    session = _Session([requests.ConnectionError(_FAKE_PRIVATE), _result()])
    with pytest.raises(BCutStageError, match="category=network_failure") as caught:
        BCutTranscriber()._post_json(session, url, {})
    assert len(session.calls) == 1
    assert _FAKE_PRIVATE not in str(caught.value)
    assert caught.value.__cause__ is None


def test_get_retries_only_transient_failures(monkeypatch) -> None:
    monkeypatch.setattr(bcut.time, "sleep", lambda _seconds: None)
    response = _Response(status=503)
    session = _Session([response, requests.Timeout(_FAKE_PRIVATE), _result()])
    assert BCutTranscriber()._await_result(session, "mock-task")["state"] == 4
    assert len(session.calls) == 3
    assert response.closed


@pytest.mark.parametrize("status", [400, 401, 403, 404, 412])
def test_get_does_not_retry_permanent_http_errors(status) -> None:
    session = _Session([_Response(status=status), _result()])
    with pytest.raises(BCutStageError):
        BCutTranscriber()._await_result(session, "mock-task")
    assert len(session.calls) == 1


def test_get_recovery_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(bcut.time, "sleep", lambda _seconds: None)
    session = _Session([requests.Timeout(_FAKE_PRIVATE)] * 3)
    with pytest.raises(BCutStageError, match="category=network_failure"):
        BCutTranscriber()._await_result(session, "mock-task")
    assert len(session.calls) == bcut.REQUEST_MAX_ATTEMPTS


def test_retry_wait_can_be_cancelled() -> None:
    event = threading.Event()

    class CancellingSession(_Session):
        def get(self, url, **kwargs):
            event.set()
            return super().get(url, **kwargs)

    session = CancellingSession([requests.Timeout(_FAKE_PRIVATE), _result()])
    with pytest.raises(BCutStageError, match="category=cancelled"):
        BCutTranscriber()._await_result(session, "mock-task", event)
    assert len(session.calls) == 1


def test_total_budget_includes_all_requests(monkeypatch) -> None:
    now = [100.0]
    monkeypatch.setattr(bcut.time, "monotonic", lambda: now[0])

    class SlowSession(_Session):
        def get(self, url, **kwargs):
            now[0] += 4.0
            return super().get(url, **kwargs)

    response = _result()
    session = SlowSession([response])
    with pytest.raises(BCutStageError, match="category=timed_out"):
        BCutTranscriber(timeout_seconds=3)._await_result(session, "mock-task")
    assert session.calls[0][2]["timeout"] <= 3
    assert response.closed


def test_upload_streams_ranges_and_rewinds_the_same_part(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(bcut.time, "sleep", lambda _seconds: None)
    audio = b"a" * (bcut.READ_BLOCK_BYTES * 3 + 7)
    path = tmp_path / "mock-audio.mp3"
    path.write_bytes(audio)
    per_size = bcut.READ_BLOCK_BYTES * 2
    spec = _meta(len(audio), per_size)

    class UploadSession(_Session):
        def __init__(self):
            super().__init__([])
            self.uploads = []
            self.read_sizes = []
            self.posts = []
            self.fail_first = True

        def post(self, url, **kwargs):
            self.posts.append((url, json.loads(kwargs["data"])))
            if url == bcut.ENDPOINT_CREATE:
                return _Response({"code": 0, "data": spec})
            return _Response({"code": 0, "data": {"download_url": _FAKE_URL}})

        def put(self, url, **kwargs):
            data = kwargs["data"]
            assert not isinstance(data, bytes)
            blocks = []
            while block := data.read():
                self.read_sizes.append(len(block))
                blocks.append(block)
                if self.fail_first:
                    self.fail_first = False
                    raise requests.Timeout(_FAKE_PRIVATE)
            self.uploads.append(b"".join(blocks))
            return _Response()

    session = UploadSession()
    result = BCutTranscriber()._upload(session, str(path))
    assert result["download_url"] == _FAKE_URL
    assert session.uploads == [audio[:per_size], audio[per_size:]]
    assert max(session.read_sizes) <= bcut.READ_BLOCK_BYTES
    assert len(session.posts) == 2
    assert session.posts[0][1]["size"] == len(audio)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("per_size", 0),
        ("per_size", True),
        ("per_size", "4"),
        ("upload_urls", []),
        ("upload_urls", ["invalid"]),
        ("resource_id", None),
    ],
)
def test_invalid_upload_spec_fails_before_any_upload(key, value) -> None:
    spec = _meta(4, 4)
    spec[key] = value
    with pytest.raises(BCutStageError, match="invalid_upload_spec"):
        BCutTranscriber()._upload_spec(spec, 4)


def test_upload_requires_etag_and_does_not_commit(tmp_path) -> None:
    path = tmp_path / "mock-audio.mp3"
    path.write_bytes(b"audio")
    session = _Session([_Response({"code": 0, "data": _meta(5, 5)}), _Response(etag="")])
    with pytest.raises(BCutStageError, match="missing_etag"):
        BCutTranscriber()._upload(session, str(path))
    assert [call[0] for call in session.calls] == ["post", "put"]


def test_missing_audio_path_is_not_disclosed() -> None:
    with pytest.raises(BCutStageError, match="audio_read_failed") as caught:
        BCutTranscriber()._upload(_Session([]), "mock-missing-audio.mp3")
    assert "mock-missing-audio.mp3" not in str(caught.value)


def test_empty_audio_does_not_create_resource(tmp_path) -> None:
    path = tmp_path / "mock-audio.mp3"
    path.write_bytes(b"")
    session = _Session([])
    with pytest.raises(BCutStageError, match="empty_audio"):
        BCutTranscriber()._upload(session, str(path))
    assert session.calls == []


@pytest.mark.parametrize(
    "payload",
    [[], {"code": 0, "data": []}, {"code": True, "data": {}}, {"code": 0, "data": {"state": None}}],
)
def test_invalid_query_responses_are_safe(payload) -> None:
    session = _Session([_Response(payload)])
    with pytest.raises(BCutStageError, match="invalid_response"):
        BCutTranscriber()._await_result(session, "mock-task")


def test_service_message_is_not_used_in_api_error() -> None:
    session = _Session([_Response({"code": -1, "message": _FAKE_PRIVATE, "data": {}})])
    with pytest.raises(BCutStageError) as caught:
        BCutTranscriber()._await_result(session, "mock-task")
    assert caught.value.api_code == -1
    assert _FAKE_PRIVATE not in str(caught.value)


@pytest.mark.parametrize(
    ("result", "category"),
    [
        ("invalid", "invalid_json"),
        ("[]", "invalid_response"),
        ('{"utterances":[]}', "empty_transcript"),
        ('{"utterances":[{"transcript":" "}]}', "empty_transcript"),
        ('{"utterances":[{"transcript":"text","start_time":"NaN"}]}', "invalid_response"),
    ],
)
def test_invalid_or_empty_transcript_is_rejected(result, category) -> None:
    transcriber = BCutTranscriber()
    with pytest.raises(BCutStageError, match=category):
        transcriber._parse_result({"result": result}, transcriber._operation(None))


def test_parse_result_preserves_text_and_timing() -> None:
    transcriber = BCutTranscriber()
    result = {
        "result": json.dumps(
            {"language": "zh", "utterances": [{"transcript": " 合成测试 ", "start_time": 10, "end_time": 50}]}
        )
    }
    transcript = transcriber._parse_result(result, transcriber._operation(None))
    assert transcript.full_text == "合成测试"
    assert transcript.segments[0].start == 0.01
    assert transcript.segments[0].end == 0.05


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_budget_is_rejected(timeout) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        BCutTranscriber(timeout_seconds=timeout)
