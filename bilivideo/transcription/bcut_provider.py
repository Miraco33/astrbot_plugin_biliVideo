"""BCut ASR with bounded, cancellable recovery and safe stage diagnostics."""

from __future__ import annotations

import io
import json
import math
import os
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import requests

from ..core.exceptions import TranscriptionError
from ..core.logging import get_logger
from ..core.types import TranscriptResult, TranscriptSegment

logger = get_logger("BiliVideo/BCut")

API_BASE = "https://member.bilibili.com/x/bcut/rubick-interface"
ENDPOINT_CREATE = f"{API_BASE}/resource/create"
ENDPOINT_COMMIT = f"{API_BASE}/resource/create/complete"
ENDPOINT_TASK = f"{API_BASE}/task"
ENDPOINT_RESULT = f"{API_BASE}/task/result"

_HEADERS = {
    "User-Agent": "Bilibili/1.0.0 (https://www.bilibili.com)",
    "Content-Type": "application/json",
}
POLL_INTERVAL_SECONDS = 1.0
POLL_MAX_TRIES = 500
REQUEST_MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 0.5
READ_BLOCK_BYTES = 64 * 1024
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
_STAGE_LABELS = {
    "read_audio": "读取音频",
    "create_resource": "申请上传",
    "upload_part": "上传音频",
    "commit_resource": "提交音频",
    "create_task": "创建转写任务",
    "query_result": "查询转写结果",
    "parse_result": "解析转写结果",
}


class BCutStageError(TranscriptionError):
    """Only fixed stage/category names and numeric codes cross the boundary."""

    def __init__(
        self,
        stage: str,
        category: str,
        *,
        http_status: int | None = None,
        api_code: int | None = None,
    ) -> None:
        self.stage = stage
        self.category = category
        self.http_status = http_status
        self.api_code = api_code
        detail = f"BCut stage={stage} category={category}"
        if http_status is not None:
            detail += f" http_status={http_status}"
        if api_code is not None:
            detail += f" api_code={api_code}"
        super().__init__(detail)
        label = _STAGE_LABELS.get(stage, "转写")
        if category == "precondition_failed":
            self.user_message = (
                f"❌ 必剪{label}被接口拒绝（HTTP 412），原因尚未确定；"
                "本次未自动重试，请稍后重试或选择其他转写服务"
            )
        elif category == "cancelled":
            self.user_message = "❌ 必剪转写已取消"
        elif category == "timed_out":
            self.user_message = f"❌ 必剪{label}超出处理时间，请稍后重试或缩短音频"
        elif category == "empty_transcript":
            self.user_message = "❌ 必剪返回空转写，未生成总结；请尝试其他视频或转写服务"
        else:
            self.user_message = f"❌ 必剪{label}失败，请稍后重试或尝试其他转写服务"


@dataclass(frozen=True)
class _Operation:
    deadline: float
    cancel_event: threading.Event | None

    def check(self, stage: str) -> float:
        _check_cancelled(self.cancel_event, stage)
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise BCutStageError(stage, "timed_out")
        return remaining

    def wait(self, seconds: float, stage: str) -> None:
        delay = min(seconds, self.check(stage))
        if self.cancel_event is None:
            time.sleep(delay)
        else:
            self.cancel_event.wait(delay)
        self.check(stage)


def _check_cancelled(cancel_event: threading.Event | None, stage: str = "query_result") -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise BCutStageError(stage, "cancelled")


def _valid_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        return parsed.scheme == "https" and bool(parsed.netloc) and not parsed.username
    except ValueError:
        return False


def _close_response(response: Any) -> None:
    close = getattr(response, "close", None)
    if close is not None:
        try:
            close()
        except (OSError, requests.RequestException):
            pass


class _UploadPart(io.RawIOBase):
    """Expose one file range; requests streams it without materialising a chunk."""

    def __init__(self, stream: Any, start: int, size: int, operation: _Operation) -> None:
        self._stream = stream
        self._start = start
        self._size = size
        self._position = 0
        self._operation = operation
        self.seek(0)

    def __len__(self) -> int:
        return self._size

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._position

    def seek(self, offset: int, whence: int = 0) -> int:
        position = offset
        if whence == 1:
            position += self._position
        elif whence == 2:
            position += self._size
        elif whence != 0:
            raise ValueError("unsupported seek mode")
        if not 0 <= position <= self._size:
            raise ValueError("invalid upload range")
        self._stream.seek(self._start + position)
        self._position = position
        return position

    def read(self, size: int = -1) -> bytes:
        self._operation.check("upload_part")
        remaining = self._size - self._position
        amount = min(remaining, READ_BLOCK_BYTES, remaining if size < 0 else size)
        try:
            block = self._stream.read(amount)
        except OSError:
            raise BCutStageError("read_audio", "audio_read_failed") from None
        if len(block) != amount:
            raise BCutStageError("read_audio", "audio_changed")
        self._position += len(block)
        return block


class BCutTranscriber:
    """Synchronous ASR; timeout is shared by upload, requests and polling."""

    def __init__(self, timeout_seconds: float = 180) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        self.timeout_seconds = float(timeout_seconds)

    def _operation(self, cancel_event: threading.Event | None) -> _Operation:
        return _Operation(time.monotonic() + self.timeout_seconds, cancel_event)

    def transcribe(
        self, file_path: str, cancel_event: threading.Event | None = None
    ) -> TranscriptResult:
        operation = self._operation(cancel_event)
        operation.check("read_audio")
        with requests.Session() as session:
            payload = self._upload(session, file_path, cancel_event, operation=operation)
            task_id = self._create_task(session, payload["download_url"], operation=operation)
            data = self._await_result(session, task_id, cancel_event, operation=operation)
        operation.check("parse_result")
        return self._parse_result(data, operation)

    def _request(
        self,
        session: requests.Session,
        method: str,
        url: str,
        stage: str,
        operation: _Operation,
        *,
        retryable: bool = False,
        **kwargs: Any,
    ) -> Any:
        attempts = REQUEST_MAX_ATTEMPTS if retryable else 1
        for attempt in range(attempts):
            remaining = operation.check(stage)
            body = kwargs.get("data")
            if isinstance(body, _UploadPart):
                body.seek(0)
            try:
                response = getattr(session, method)(url, timeout=min(15.0, remaining), **kwargs)
            except (requests.ConnectionError, requests.Timeout):
                error = BCutStageError(stage, "network_failure")
            except requests.RequestException:
                raise BCutStageError(stage, "request_failed") from None
            else:
                try:
                    operation.check(stage)
                    status = getattr(response, "status_code", 200)
                    if status == 412:
                        raise BCutStageError(stage, "precondition_failed", http_status=412)
                    if not isinstance(status, int) or isinstance(status, bool):
                        raise BCutStageError(stage, "invalid_response")
                    if status >= 400:
                        error = BCutStageError(stage, "http_error", http_status=status)
                        if status not in _RETRYABLE_STATUS:
                            raise error
                    elif not 200 <= status < 300:
                        raise BCutStageError(stage, "http_error", http_status=status)
                    else:
                        try:
                            response.raise_for_status()
                        except requests.RequestException:
                            raise BCutStageError(stage, "request_failed") from None
                        return response
                except BCutStageError:
                    _close_response(response)
                    raise
                _close_response(response)
            if attempt + 1 == attempts:
                raise error from None
            operation.wait(RETRY_DELAY_SECONDS * (2**attempt), stage)
        raise BCutStageError(stage, "request_failed")

    def _json_data(self, response: Any, stage: str) -> dict[str, Any]:
        try:
            try:
                payload = response.json()
            except (ValueError, TypeError):
                raise BCutStageError(stage, "invalid_json") from None
        finally:
            _close_response(response)
        if not isinstance(payload, dict):
            raise BCutStageError(stage, "invalid_response")
        code = payload.get("code")
        if isinstance(code, bool) or not isinstance(code, int):
            raise BCutStageError(stage, "invalid_response")
        if code != 0:
            # Never copy service message/body: those may contain identifiers.
            safe_code = code if abs(code) <= 1_000_000_000 else None
            raise BCutStageError(stage, "api_error", api_code=safe_code)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise BCutStageError(stage, "invalid_response")
        return data

    def _upload(
        self,
        session: requests.Session,
        file_path: str,
        cancel_event: threading.Event | None = None,
        *,
        operation: _Operation | None = None,
    ) -> dict[str, Any]:
        operation = operation or self._operation(cancel_event)
        operation.check("read_audio")
        try:
            with open(file_path, "rb") as stream:
                size = os.fstat(stream.fileno()).st_size
                if size <= 0:
                    raise BCutStageError("read_audio", "empty_audio")
                meta = self._post_json(
                    session,
                    ENDPOINT_CREATE,
                    {
                        "type": 2,
                        "name": "audio.mp3",
                        "size": size,
                        "ResourceFileType": "mp3",
                        "model_id": "8",
                    },
                    operation=operation,
                )
                urls, per_size = self._upload_spec(meta, size)
                etags: list[str] = []
                for index, url in enumerate(urls):
                    operation.check("upload_part")
                    start = index * per_size
                    part = _UploadPart(stream, start, min(per_size, size - start), operation)
                    response = self._request(
                        session,
                        "put",
                        url,
                        "upload_part",
                        operation,
                        retryable=True,
                        data=part,
                        headers={"Content-Type": "application/octet-stream"},
                    )
                    try:
                        etag = response.headers.get("Etag", "")
                    finally:
                        _close_response(response)
                    if not isinstance(etag, str) or not etag.strip('" '):
                        raise BCutStageError("upload_part", "missing_etag")
                    etags.append(etag.strip('" '))
                if os.fstat(stream.fileno()).st_size != size:
                    raise BCutStageError("read_audio", "audio_changed")
        except OSError:
            raise BCutStageError("read_audio", "audio_read_failed") from None

        commit = self._post_json(
            session,
            ENDPOINT_COMMIT,
            {
                "InBossKey": meta["in_boss_key"],
                "ResourceId": meta["resource_id"],
                "Etags": ",".join(etags),
                "UploadId": meta["upload_id"],
                "model_id": "8",
            },
            operation=operation,
        )
        if not _valid_url(commit.get("download_url")):
            raise BCutStageError("commit_resource", "invalid_response")
        return commit

    def _upload_spec(self, meta: dict[str, Any], size: int) -> tuple[list[str], int]:
        urls = meta.get("upload_urls")
        per_size = meta.get("per_size")
        if (
            not isinstance(per_size, int)
            or isinstance(per_size, bool)
            or per_size <= 0
            or not isinstance(urls, list)
            or len(urls) != (size + per_size - 1) // per_size
            or not all(_valid_url(url) for url in urls)
        ):
            raise BCutStageError("create_resource", "invalid_upload_spec")
        for key in ("in_boss_key", "resource_id", "upload_id"):
            value = meta.get(key)
            if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
                raise BCutStageError("create_resource", "invalid_upload_spec")
        return urls, per_size

    def _create_task(
        self,
        session: requests.Session,
        download_url: str,
        *,
        operation: _Operation | None = None,
    ) -> str:
        operation = operation or self._operation(None)
        if not _valid_url(download_url):
            raise BCutStageError("create_task", "invalid_response")
        payload = self._post_json(
            session,
            ENDPOINT_TASK,
            {"resource": download_url, "model_id": "8"},
            operation=operation,
        )
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise BCutStageError("create_task", "invalid_response")
        return task_id

    def _await_result(
        self,
        session: requests.Session,
        task_id: str,
        cancel_event: threading.Event | None = None,
        *,
        operation: _Operation | None = None,
    ) -> dict[str, Any]:
        operation = operation or self._operation(cancel_event)
        for attempt in range(POLL_MAX_TRIES):
            response = self._request(
                session,
                "get",
                ENDPOINT_RESULT,
                "query_result",
                operation,
                retryable=True,
                params={"model_id": 7, "task_id": task_id},
                headers=_HEADERS,
            )
            data = self._json_data(response, "query_result")
            state = data.get("state")
            if isinstance(state, bool) or not isinstance(state, int) or state < 0:
                raise BCutStageError("query_result", "invalid_response")
            if state == 4:
                return data
            if state == 3:
                raise BCutStageError("query_result", "task_failed")
            if attempt and attempt % 30 == 0:
                logger.info(f"BCut polling {attempt}/{POLL_MAX_TRIES}")
            operation.wait(POLL_INTERVAL_SECONDS, "query_result")
        raise BCutStageError("query_result", "timed_out")

    def _post_json(
        self,
        session: requests.Session,
        url: str,
        body: dict[str, Any],
        *,
        operation: _Operation | None = None,
    ) -> dict[str, Any]:
        operation = operation or self._operation(None)
        stage = {
            ENDPOINT_CREATE: "create_resource",
            ENDPOINT_COMMIT: "commit_resource",
            ENDPOINT_TASK: "create_task",
        }[url]
        # POST operations may have succeeded before a connection failure.
        # Retrying them blindly can create duplicate resources or tasks.
        response = self._request(
            session, "post", url, stage, operation, data=json.dumps(body), headers=_HEADERS
        )
        return self._json_data(response, stage)

    def _parse_result(self, data: dict[str, Any], operation: _Operation) -> TranscriptResult:
        try:
            result_json = json.loads(data["result"])
        except (KeyError, ValueError, TypeError):
            raise BCutStageError("parse_result", "invalid_json") from None
        if not isinstance(result_json, dict) or not isinstance(result_json.get("utterances"), list):
            raise BCutStageError("parse_result", "invalid_response")
        segments: list[TranscriptSegment] = []
        for utterance in result_json["utterances"]:
            operation.check("parse_result")
            if not isinstance(utterance, dict):
                raise BCutStageError("parse_result", "invalid_response")
            text = utterance.get("transcript") or ""
            if not isinstance(text, str):
                raise BCutStageError("parse_result", "invalid_response")
            if not text.strip():
                continue
            try:
                start = float(utterance.get("start_time", 0)) / 1000.0
                end = float(utterance.get("end_time", 0)) / 1000.0
            except (TypeError, ValueError, OverflowError):
                raise BCutStageError("parse_result", "invalid_response") from None
            if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
                raise BCutStageError("parse_result", "invalid_response")
            segments.append(TranscriptSegment(start=start, end=end, text=text.strip()))
        if not segments:
            raise BCutStageError("parse_result", "empty_transcript")
        language = result_json.get("language", "zh")
        return TranscriptResult(
            language=language if isinstance(language, str) else "zh",
            full_text=" ".join(segment.text for segment in segments),
            segments=tuple(segments),
            raw=result_json,
        )
