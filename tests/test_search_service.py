"""SearchService batch accounting tests."""

from __future__ import annotations

import pytest

from bilivideo.search import SearchService


class _HTTP:
    pass


class _Pipeline:
    pass


@pytest.mark.asyncio
async def test_unexpected_process_exception_counts_as_failed(tmp_path, monkeypatch) -> None:
    service = SearchService(data_dir=tmp_path, http_client=_HTTP(), pipeline=_Pipeline())

    async def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "_process_one", _boom)
    progress: list[dict] = []

    async def _progress(payload: dict) -> None:
        progress.append(payload)

    result = await service.process_bv_list(
        bv_list=["BV1xx411c7mD"],
        folder_name="case",
        progress_callback=_progress,
    )

    assert result.total_count == 1
    assert result.success_count == 0
    assert result.failed_count == 1
    assert result.videos[0].bvid == "BV1xx411c7mD"
    assert "RuntimeError" in result.videos[0].error
    assert "boom" not in result.videos[0].error
    assert progress[-1]["is_last"] is True



@pytest.mark.asyncio
async def test_invalid_bvid_fails_without_api_request(tmp_path, monkeypatch) -> None:
    from unittest.mock import AsyncMock

    from bilivideo import search

    get_info = AsyncMock()
    monkeypatch.setattr(search, "get_video_info", get_info)
    service = SearchService(data_dir=tmp_path, http_client=_HTTP(), pipeline=_Pipeline())
    result = await service.process_bv_list(
        bv_list=["BV1xx411c7mDextra"], folder_name="synthetic",
    )
    assert result.failed_count == 1
    assert result.videos[0].error == "invalid_bvid"
    get_info.assert_not_called()


def test_transcript_without_segments_keeps_full_text() -> None:
    from bilivideo.core.types import TranscriptResult
    from bilivideo.search import _format_transcript

    transcript = TranscriptResult(language=None, full_text="synthetic text", segments=())
    assert _format_transcript(transcript) == "synthetic text"
