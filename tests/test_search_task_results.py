"""Session-isolated background results; no network, account or model calls."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bilivideo.tools import registry


def _ctx(origin="test:GroupMessage:1"):
    return SimpleNamespace(
        context=SimpleNamespace(event=SimpleNamespace(unified_msg_origin=origin))
    )


def _video(text="synthetic transcript", *, success=True):
    return SimpleNamespace(
        bvid="BV1xx411c7mD", info=SimpleNamespace(title="synthetic"),
        transcript=text, success=success,
    )


def _batch(videos):
    success = sum(video.success for video in videos)
    return SimpleNamespace(
        videos=videos, success_count=success, failed_count=len(videos) - success,
    )


def _services():
    config = SimpleNamespace(
        group_list=[], access_mode="blacklist", default_download_count=1,
        search_show_progress=False, search_max_concurrent=1, prefer_subtitle=True,
        download_quality="fast", subtitle_langs=None,
    )
    return SimpleNamespace(config=config, search_service=SimpleNamespace())


@pytest.fixture(autouse=True)
def _framework(monkeypatch):
    monkeypatch.setattr(registry, "FunctionTool", object)
    monkeypatch.setattr(registry, "_TOOL_WAIT_SECONDS", 0)
    monkeypatch.setattr(registry, "_safe_send_text", AsyncMock())
    monkeypatch.setattr(registry, "_send_combined_summary", AsyncMock(return_value="synthetic summary"))


async def test_background_content_is_readable_only_in_originating_session():
    services = _services()
    gate = asyncio.Event()

    async def process(**kwargs):
        await gate.wait()
        return _batch([_video()])

    services.search_service.process_bv_list = process
    tool = registry._make_search_download_tool(services, SimpleNamespace())
    started = json.loads(await tool.call(
        _ctx(), folder_name="synthetic", bv_list=["BV1xx411c7mD"],
    ))
    assert started["status"] == "running"
    task_id = started["task_id"]
    result_tool = registry._make_search_result_tool(services)
    denied = await result_tool.call(_ctx("test:GroupMessage:2"), task_id=task_id)
    assert json.loads(denied)["error"] == "task_not_found_or_expired"
    gate.set()
    await registry._download_results(services).find(task_id, "test:GroupMessage:1").task
    result = json.loads(await result_tool.call(_ctx(), task_id=task_id))
    assert result["status"] == "completed"
    assert result["content"]["text"] == "synthetic transcript"
    assert result["summary"] == "synthetic summary"
    assert "folder_path" not in result
    assert "origin" not in result


async def test_fast_download_returns_text_in_original_tool_result(monkeypatch):
    monkeypatch.setattr(registry, "_TOOL_WAIT_SECONDS", 1)
    services = _services()
    services.search_service.process_bv_list = AsyncMock(return_value=_batch([_video("fast text")]))
    started = json.loads(await registry._make_search_download_tool(
        services, SimpleNamespace()
    ).call(_ctx(), folder_name="synthetic", bv_list=["BV1xx411c7mD"]))
    assert started["content"]["text"] == "fast text"


async def test_failed_background_task_has_queryable_safe_error():
    services = _services()
    services.search_service.process_bv_list = AsyncMock(side_effect=RuntimeError("private details"))
    started = json.loads(await registry._make_search_download_tool(
        services, SimpleNamespace()
    ).call(_ctx(), folder_name="synthetic", bv_list=["BV1xx411c7mD"]))
    item = registry._download_results(services).find(started["task_id"], "test:GroupMessage:1")
    await item.task
    payload = registry._result_payload(item)
    assert json.loads(payload)["status"] == "failed"
    assert "RuntimeError" in payload
    assert "private details" not in payload


async def test_concurrent_sessions_do_not_cancel_each_other():
    services = _services()
    gate = asyncio.Event()

    async def process(**kwargs):
        await gate.wait()
        return _batch([_video()])

    services.search_service.process_bv_list = process
    download = registry._make_search_download_tool(services, SimpleNamespace())
    first = json.loads(await download.call(
        _ctx(), folder_name="one", bv_list=["BV1xx411c7mD"],
    ))
    second = json.loads(await download.call(
        _ctx("test:GroupMessage:2"), folder_name="two", bv_list=["BV1xx411c7mD"],
    ))
    store = registry._download_results(services)
    assert first["task_id"] != second["task_id"]
    assert not store.find(first["task_id"], "test:GroupMessage:1").task.cancelled()
    registry.cancel_search_tasks(services)
    await asyncio.gather(*(item.task for item in store.records.values()), return_exceptions=True)
    assert all(item.status == "cancelled" for item in store.records.values())


async def test_invalid_id_is_rejected_before_background_processing():
    services = _services()
    services.search_service.process_bv_list = AsyncMock()
    tool = registry._make_search_download_tool(services, SimpleNamespace())
    for invalid in ("BV1xx", "BV1xx411c7mDextra", 123, "https://example.com/video/BV1xx411c7mD"):
        result = await tool.call(_ctx(), folder_name="synthetic", bv_list=[invalid])
        assert "错误" in result
    services.search_service.process_bv_list.assert_not_called()


def test_cache_expiration_capacity_and_text_bounds(monkeypatch):
    store = registry._DownloadResults()
    item = store.create("test:GroupMessage:1")
    registry._DownloadResults.finish(item, _batch([_video("x" * 250_000)]))
    payload = json.loads(registry._result_payload(item, offset=12000, length=20000))
    assert len(payload["content"]["text"]) == 12000
    assert payload["content"]["next_offset"] == 24000
    assert payload["videos"][0]["truncated"] is True
    assert payload["videos"][0]["transcript_chars"] == registry._TASK_TEXT_LIMIT
    assert payload["videos"][0]["original_chars"] == 250_000
    for index in range(30):
        created = store.create(f"test:GroupMessage:{index + 2}")
        assert created is not None
        created.status = "completed"
    assert len(store.records) == registry._TASK_LIMIT
    now = time.monotonic()
    monkeypatch.setattr(registry.time, "monotonic", lambda: now + registry._TASK_TTL_SECONDS + 1)
    assert store.find(item.task_id, item.origin) is None
    assert not store.records


def test_partial_result_preserves_success_and_failure_status():
    item = registry._DownloadResults().create("test:GroupMessage:1")
    registry._DownloadResults.finish(item, _batch([_video("available"), _video("", success=False)]))
    payload = json.loads(registry._result_payload(item))
    assert payload["status"] == "partial"
    assert payload["content"]["text"] == "available"
    assert payload["videos"][1]["error"] == "video_processing_failed"
