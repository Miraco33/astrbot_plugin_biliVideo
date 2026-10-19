"""Malformed BVs and redirect domains must not reach metadata requests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bilivideo.handlers import auto_detect
from bilivideo.parsing.message_router import MessageContext


@pytest.mark.asyncio
async def test_malformed_bvid_text_does_not_resolve():
    services = SimpleNamespace(http_client=SimpleNamespace(follow_redirect=AsyncMock()))
    ctx = MessageContext(plain_text="BV1xx411c7mDextra", raw_message="BV1xx411c7mDextra")
    assert await auto_detect._resolve_bvid(services, ctx, allow_full_text=True) is None
    services.http_client.follow_redirect.assert_not_called()


@pytest.mark.asyncio
async def test_external_redirect_bvid_is_not_treated_as_bilibili():
    http = SimpleNamespace(follow_redirect=AsyncMock(
        return_value="https://example.com/video/BV1xx411c7mD"
    ))
    ctx = MessageContext(plain_text="https://b23.tv/synthetic")
    assert await auto_detect._resolve_bvid(
        SimpleNamespace(http_client=http), ctx, allow_full_text=True
    ) is None


@pytest.mark.asyncio
async def test_napcat_json_card_resolves_bvid():
    from bilivideo.parsing.message_router import parse_event

    component = SimpleNamespace(
        type="json", data={"meta": {"detail_1": {"qqdocurl": "https://b23.tv/synthetic"}}}
    )
    event = SimpleNamespace(
        message_str="",
        message_obj=SimpleNamespace(message=[component], raw_message=None),
    )
    http = SimpleNamespace(follow_redirect=AsyncMock(
        return_value="https://www.bilibili.com/video/BV1xx411c7mD"
    ))
    ctx = parse_event(event)
    assert await auto_detect._resolve_bvid(
        SimpleNamespace(http_client=http), ctx, allow_full_text=True
    ) == "BV1xx411c7mD"
