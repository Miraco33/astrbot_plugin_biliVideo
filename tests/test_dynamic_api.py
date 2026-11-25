"""Space dynamics use one authenticated client request and a bounded page."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from bilivideo.api.client import BilibiliHTTPClient
from bilivideo.api.endpoints import get_latest_dynamics
from bilivideo.auth.cookies import REQUIRED_COOKIE_KEY
from bilivideo.core.constants import ENDPOINT_USER_DYNAMICS
from bilivideo.core.exceptions import BiliVideoError, NotLoggedInError, RiskControlError


def _item(
    dynamic_id: str,
    pubdate: int,
    *,
    kind: str = "DYNAMIC_TYPE_WORD",
    text: str = "",
    major: dict | None = None,
) -> dict:
    return {
        "id_str": dynamic_id,
        "type": kind,
        "visible": True,
        "modules": {
            "module_author": {"pub_ts": pubdate},
            "module_dynamic": {"desc": {"text": text} if text else None, "major": major},
        },
    }


def _client(payload: object) -> AsyncMock:
    client = AsyncMock(BilibiliHTTPClient)
    client.cookies = {REQUIRED_COOKIE_KEY: "mock-session"}
    client.request_json.return_value = payload
    return client


async def test_space_dynamics_parse_and_deduplicate_by_id() -> None:
    pinned = _item("101", 100, text="旧动态")
    pinned["modules"]["module_tag"] = {"text": "置顶"}
    image = _item(
        "102",
        101,
        kind="DYNAMIC_TYPE_DRAW",
        major={"type": "MAJOR_TYPE_OPUS", "opus": {"summary": {"text": "   图文正文\n第二行  "}}},
    )
    image_only = _item(
        "103", 102, kind="DYNAMIC_TYPE_DRAW", major={"type": "MAJOR_TYPE_DRAW", "draw": {"items": [{}]}}
    )
    article = _item(
        "104",
        102,
        kind="DYNAMIC_TYPE_ARTICLE",
        major={"type": "MAJOR_TYPE_ARTICLE", "article": {"title": "专栏标题", "desc": "专栏摘要"}},
    )
    forward = _item("105", 103, kind="DYNAMIC_TYPE_FORWARD", text="转发语")
    forward["orig"] = _item("999", 10, text="原文不应进入推送")
    no_comment = _item("106", 104, kind="DYNAMIC_TYPE_FORWARD")
    video = _item(
        "107",
        105,
        kind="DYNAMIC_TYPE_AV",
        major={"type": "MAJOR_TYPE_ARCHIVE", "archive": {"title": "视频标题", "bvid": "BV1xx411c7mD"}},
    )
    items = [pinned, image, image_only, article, forward, no_comment, video, _item("101", 100, text="旧动态")]
    client = _client({"code": 0, "data": {"items": items}})

    result = await get_latest_dynamics(client, "123")

    assert [(item.dynamic_id, item.pubdate, item.text, item.bvid) for item in result] == [
        ("107", 105, "视频标题", "BV1xx411c7mD"),
        ("106", 104, "转发动态", ""),
        ("105", 103, "转发语", ""),
        ("104", 102, "专栏标题\n专栏摘要", ""),
        ("103", 102, "", ""),
        ("102", 101, "图文正文\n第二行", ""),
        ("101", 100, "旧动态", ""),
    ]
    assert result[0].kind == "DYNAMIC_TYPE_AV"
    assert all(item.url == f"https://t.bilibili.com/{item.dynamic_id}" for item in result)
    client.request_json.assert_awaited_once_with(
        "GET",
        ENDPOINT_USER_DYNAMICS,
        params={"host_mid": "123", "offset": "", "platform": "web", "features": "itemOpusStyle"},
        headers={"Referer": "https://space.bilibili.com/123/dynamic"},
    )


async def test_space_dynamics_skip_restricted_and_unavailable_posts() -> None:
    hidden = {"visible": False}
    invalid = {"type": "DYNAMIC_TYPE_NONE"}
    supporter = _item("101", 100, text="会员内容")
    supporter["modules"]["module_author"]["icon_badge"] = {"text": "充电专属"}
    lottery = _item("102", 101, text="专属抽奖")
    lottery["modules"]["module_dynamic"]["additional"] = {"type": "ADDITIONAL_TYPE_UPOWER_LOTTERY"}
    private = _item("103", 102, kind="DYNAMIC_TYPE_PRIVATE", text="私密内容")
    other = _item("104", 103, text="公开")
    client = _client({"code": 0, "data": {"items": [hidden, invalid, supporter, lottery, private, other]}})

    assert [item.dynamic_id for item in await get_latest_dynamics(client, "123")] == ["104"]


async def test_space_dynamics_limit_applied_after_sorting_single_page() -> None:
    items = [_item(str(index), index) for index in range(1, 26)]
    client = _client({"code": 0, "data": {"items": items, "has_more": True}})

    result = await get_latest_dynamics(client, "123", count=99)

    assert [item.pubdate for item in result] == list(range(25, 5, -1))
    client.request_json.assert_awaited_once()


async def test_space_dynamics_empty_page_is_valid() -> None:
    client = _client({"code": 0, "data": {"items": [], "has_more": False}})

    assert await get_latest_dynamics(client, "123") == []


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"code": "0", "data": {"items": []}},
        {"code": 0, "data": {}},
        {"code": 0, "data": {"items": [], "has_more": True}},
        {"code": 0, "data": {"items": [None]}},
        {"code": 0, "data": {"items": [{"id_str": "10", "type": "DYNAMIC_TYPE_WORD"}]}},
        {"code": 0, "data": {"items": [_item("1/path", 100)]}},
        {"code": 0, "data": {"items": [_item("123", 0), _item("124", 100)]}},
    ],
)
async def test_space_dynamics_reject_malformed_page(payload: object) -> None:
    with pytest.raises(BiliVideoError):
        await get_latest_dynamics(_client(payload), "123")


async def test_space_dynamics_propagate_risk_control_without_retry() -> None:
    client = _client(None)
    client.request_json.side_effect = RiskControlError()

    with pytest.raises(RiskControlError):
        await get_latest_dynamics(client, "123")

    client.request_json.assert_awaited_once()


async def test_space_dynamics_validate_uid_before_request() -> None:
    client = _client(None)

    with pytest.raises(BiliVideoError):
        await get_latest_dynamics(client, "123/path")

    client.request_json.assert_not_awaited()


async def test_space_dynamics_require_existing_login_cookie() -> None:
    client = _client(None)
    client.cookies = {}

    with pytest.raises(NotLoggedInError):
        await get_latest_dynamics(client, "123")

    client.request_json.assert_not_awaited()
