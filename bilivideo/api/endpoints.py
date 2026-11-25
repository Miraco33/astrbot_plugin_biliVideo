"""High-level Bilibili API helpers.

Each function takes a `BilibiliHTTPClient` (allowing dependency-injection
in tests) and returns typed dataclasses instead of raw dicts. Errors
propagate as exceptions from `core.exceptions`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from ..auth.cookies import REQUIRED_COOKIE_KEY
from ..cache.lru_ttl import LRUTTLCache
from ..core.constants import (
    BV_REGEX,
    ENDPOINT_SEARCH_TYPE,
    ENDPOINT_SEARCH_TYPE_WBI,
    ENDPOINT_USER_DYNAMICS,
    ENDPOINT_USER_INFO,
    ENDPOINT_USER_VIDEOS,
    ENDPOINT_VIEW,
    VIDEO_INFO_CACHE_MAX,
    VIDEO_INFO_CACHE_TTL_SECONDS,
)
from ..core.exceptions import BilibiliAPIError, BiliVideoError, NetworkError, NotLoggedInError
from ..core.logging import get_logger
from ..core.types import (
    LatestVideo,
    SearchResult,
    SearchVideoItem,
    UploaderInfo,
    VideoInfo,
)
from .client import BilibiliHTTPClient
from .wbi import sign_params

logger = get_logger("BiliVideo/API")

_HIGHLIGHT_RE = re.compile(r'</?em[^>]*>')
_BILIBILI_ID_RE = re.compile(r"[0-9]{1,20}")
_DYNAMIC_PAGE_LIMIT = 20
_NON_POST_DYNAMIC_TYPES = {"DYNAMIC_TYPE_NONE", "DYNAMIC_TYPE_AD", "DYNAMIC_TYPE_BANNER"}


@dataclass(frozen=True, slots=True)
class LatestDynamic:
    dynamic_id: str
    pubdate: int
    text: str
    url: str
    kind: str
    bvid: str = ""


def _strip_highlight(text: str) -> str:
    """Remove `<em class="keyword">` tags wrapping the search query."""

    return _HIGHLIGHT_RE.sub("", text or "")


def _normalize_pic(pic: str) -> str:
    if pic.startswith("//"):
        return "https:" + pic
    return pic


# Cache scope: per-process. We don't bind it to a client instance so
# different short-lived BilibiliHTTPClients (e.g. in tests) still benefit.
_video_info_cache: LRUTTLCache[str, VideoInfo] = LRUTTLCache(
    max_size=VIDEO_INFO_CACHE_MAX, ttl_seconds=VIDEO_INFO_CACHE_TTL_SECONDS
)


def video_info_cache_size() -> int:
    return len(_video_info_cache)


async def clear_video_info_cache() -> None:
    await _video_info_cache.clear()


# ──────────────────────────── reading ───────────────────────────────


async def get_video_info(client: BilibiliHTTPClient, bvid: str) -> VideoInfo:
    """Fetch /x/web-interface/view, returning a typed `VideoInfo`."""

    async def fetch() -> VideoInfo:
        payload = await client.request_json("GET", ENDPOINT_VIEW, params={"bvid": bvid})
        data = payload.get("data") or {}
        owner = data.get("owner") or {}
        stat = data.get("stat") or {}
        return VideoInfo(
            bvid=str(data.get("bvid") or bvid),
            title=str(data.get("title", "")),
            pic=_normalize_pic(str(data.get("pic", ""))),
            desc=str(data.get("desc", "")),
            pubdate=int(data.get("pubdate", 0) or 0),
            duration=int(data.get("duration", 0) or 0),
            owner_name=str(owner.get("name", "未知")),
            owner_mid=str(owner.get("mid", "")),
            view=int(stat.get("view", 0) or 0),
            danmaku=int(stat.get("danmaku", 0) or 0),
            like=int(stat.get("like", 0) or 0),
            raw=data,
        )

    return await _video_info_cache.get_or_set(bvid, fetch)


async def get_uploader_info(client: BilibiliHTTPClient, mid: str) -> UploaderInfo | None:
    signed = await sign_params({"mid": mid}, client=client)
    if "w_rid" not in signed:
        logger.warning(f"get_uploader_info({mid}) skipped: WBI signing unavailable")
        return None
    try:
        payload = await client.request_json("GET", ENDPOINT_USER_INFO, params=signed)
    except (BilibiliAPIError, NetworkError) as exc:
        logger.warning(f"get_uploader_info({mid}) failed: {exc}")
        return None
    data = payload.get("data") or {}
    return UploaderInfo(
        mid=str(data.get("mid", mid)),
        name=str(data.get("name", "未知")),
        face=str(data.get("face", "")),
        sign=str(data.get("sign", "")),
    )


async def get_latest_videos(
    client: BilibiliHTTPClient, mid: str, *, count: int = 5
) -> list[LatestVideo]:
    params = {"mid": mid, "ps": count, "pn": 1, "order": "pubdate"}
    signed = await sign_params(params, client=client)
    if "w_rid" not in signed:
        logger.warning(f"get_latest_videos({mid}) skipped: WBI signing unavailable")
        return []
    try:
        payload = await client.request_json("GET", ENDPOINT_USER_VIDEOS, params=signed)
    except (BilibiliAPIError, NetworkError) as exc:
        logger.warning(f"get_latest_videos({mid}) failed: {exc}")
        return []

    data = payload.get("data") or {}
    vlist = ((data.get("list") or {}).get("vlist") or [])
    return [
        LatestVideo(
            bvid=str(v.get("bvid", "")),
            title=str(v.get("title", "")),
            pic=_normalize_pic(str(v.get("pic", ""))),
            pubdate=int(v.get("created", 0) or 0),
            duration=str(v.get("length", "")),
            description=str(v.get("description", "")),
        )
        for v in vlist[:count]
    ]


def _as_mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _as_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _parse_space_dynamic(item: object) -> LatestDynamic | None:
    if not isinstance(item, Mapping):
        raise BiliVideoError("B 站动态接口返回了无效条目")

    kind = _as_text(item.get("type"))
    if kind in _NON_POST_DYNAMIC_TYPES:
        return None
    visible = item.get("visible")
    if not isinstance(visible, bool):
        raise BiliVideoError("B 站动态接口返回了无效的可见状态")
    if not visible:
        return None

    modules = _as_mapping(item.get("modules"))
    author = _as_mapping(modules.get("module_author"))
    content = _as_mapping(modules.get("module_dynamic"))
    major = _as_mapping(content.get("major"))
    major_type = _as_text(major.get("type"))
    archive = _as_mapping(major.get("archive"))
    badges = (_as_mapping(author.get("icon_badge")), _as_mapping(archive.get("badge")))
    badge_labels = {_as_text(badge.get("text")) for badge in badges}
    additional = _as_mapping(content.get("additional"))
    if (
        badge_labels.intersection({"充电专属", "充电专享"})
        or additional.get("type") == "ADDITIONAL_TYPE_UPOWER_LOTTERY"
        or any(marker in kind or marker in major_type for marker in ("ONLYFANS", "PRIVATE", "UPOWER"))
    ):
        return None

    dynamic_id = _as_text(item.get("id_str"))
    pubdate = author.get("pub_ts")
    if (
        not _BILIBILI_ID_RE.fullmatch(dynamic_id)
        or dynamic_id.startswith("0")
        or type(pubdate) is not int
        or pubdate <= 0
        or not kind.startswith("DYNAMIC_TYPE_")
        or not isinstance(modules.get("module_dynamic"), Mapping)
    ):
        raise BiliVideoError("B 站动态接口条目缺少有效的编号、时间、类型或正文模块")

    desc = _as_text(_as_mapping(content.get("desc")).get("text"))
    if kind == "DYNAMIC_TYPE_FORWARD":
        # Let Bilibili enforce the original post's separate visibility limits.
        text = desc or "转发动态"
    else:
        major_card = _as_mapping(major.get(major_type.removeprefix("MAJOR_TYPE_").lower()))
        title = _as_text(major_card.get("title"))
        summary = _as_text(_as_mapping(major_card.get("summary")).get("text"))
        body = summary or desc or _as_text(major_card.get("desc"))
        text = "\n".join(dict.fromkeys(part for part in (title, body) if part))
    bvid = _as_text(archive.get("bvid"))
    return LatestDynamic(
        dynamic_id=dynamic_id,
        pubdate=pubdate,
        text=text,
        url=f"https://t.bilibili.com/{dynamic_id}",
        kind=kind,
        bvid=bvid if kind == "DYNAMIC_TYPE_AV" and BV_REGEX.fullmatch(bvid) else "",
    )


async def get_latest_dynamics(
    client: BilibiliHTTPClient, mid: str, *, count: int = _DYNAMIC_PAGE_LIMIT
) -> list[LatestDynamic]:
    """Read a single page of space dynamics, with newest published items first."""
    if not _BILIBILI_ID_RE.fullmatch(mid) or not mid.lstrip("0"):
        raise BiliVideoError("B 站动态接口需要有效的 UP 主 UID")
    if count <= 0:
        return []
    if not client.cookies.get(REQUIRED_COOKIE_KEY):
        raise NotLoggedInError()
    payload = await client.request_json(
        "GET",
        ENDPOINT_USER_DYNAMICS,
        params={"host_mid": mid, "offset": "", "platform": "web", "features": "itemOpusStyle"},
        headers={"Referer": f"https://space.bilibili.com/{mid}/dynamic"},
    )
    response = _as_mapping(payload)
    code = response.get("code")
    if type(code) is not int or code != 0:
        raise BiliVideoError("B 站动态接口返回了无效状态")
    data = _as_mapping(response.get("data"))
    items = data.get("items")
    if not isinstance(items, list) or (not items and data.get("has_more") is True):
        raise BiliVideoError("B 站动态接口返回了无效列表")

    unique: dict[str, LatestDynamic] = {}
    for item in items:
        dynamic = _parse_space_dynamic(item)
        if dynamic is not None:
            unique.setdefault(dynamic.dynamic_id, dynamic)
    ordered = sorted(unique.values(), key=lambda item: (item.pubdate, int(item.dynamic_id)), reverse=True)
    return ordered[:min(count, _DYNAMIC_PAGE_LIMIT)]


async def search_uploader_by_name(
    client: BilibiliHTTPClient, keyword: str
) -> UploaderInfo | None:
    """Search by name, falling back to the legacy endpoint when WBI is rejected."""

    params: Mapping[str, object] = {
        "search_type": "bili_user",
        "keyword": keyword,
        "page": 1,
        "order": "fans",
        "order_sort": 0,
    }
    signed = await sign_params(params, client=client)

    endpoint_candidates = (
        ((ENDPOINT_SEARCH_TYPE_WBI, signed), (ENDPOINT_SEARCH_TYPE, params))
        if "w_rid" in signed
        else ((ENDPOINT_SEARCH_TYPE, params),)
    )

    for endpoint, qparams in endpoint_candidates:
        try:
            logger.debug(f"search_uploader keyword={keyword} endpoint={endpoint}")
            payload = await client.request_json("GET", endpoint, params=qparams)
        except BiliVideoError as exc:
            logger.warning(f"search_uploader_by_name endpoint={endpoint} failed: {exc}; fallback")
            continue

        results = ((payload.get("data") or {}).get("result") or [])
        if not results:
            continue
        # prefer exact match
        for r in results:
            uname = _strip_highlight(str(r.get("uname", "")))
            if uname == keyword:
                return UploaderInfo(mid=str(r.get("mid", "")), name=uname)
        first = results[0]
        return UploaderInfo(
            mid=str(first.get("mid", "")),
            name=_strip_highlight(str(first.get("uname", "未知"))),
        )

    return None


async def search_videos(
    client: BilibiliHTTPClient,
    keyword: str,
    *,
    page: int = 1,
    page_size: int = 20,
    order: str = "totalrank",
    duration: int = 0,
    tids: int = 0,
) -> SearchResult | None:
    params = {
        "search_type": "video",
        "keyword": keyword,
        "page": page,
        "page_size": page_size,
        "order": order,
        "duration": duration,
        "tids": tids,
    }
    signed = await sign_params(params, client=client)

    payload: dict[str, object] | None = None
    endpoint_candidates = (
        ((ENDPOINT_SEARCH_TYPE_WBI, signed), (ENDPOINT_SEARCH_TYPE, params))
        if "w_rid" in signed
        else ((ENDPOINT_SEARCH_TYPE, params),)
    )
    for endpoint, qparams in endpoint_candidates:
        try:
            logger.debug(f"search_videos keyword={keyword} endpoint={endpoint} page={page}")
            candidate = await client.request_json("GET", endpoint, params=qparams)
        except BiliVideoError as exc:
            logger.warning(f"search_videos endpoint={endpoint} failed: {exc}; fallback")
            continue
        raw = ((candidate.get("data") or {}).get("result") or [])
        if raw:
            payload = candidate
            break
        logger.debug(f"search_videos endpoint={endpoint} returned empty result; fallback")
        payload = candidate
    if payload is None:
        return None

    data = payload.get("data") or {}
    raw_results = data.get("result") or []
    items: list[SearchVideoItem] = []
    for r in raw_results:
        if r.get("type") != "video":
            continue
        items.append(
            SearchVideoItem(
                bvid=str(r.get("bvid", "")),
                aid=int(r.get("aid", 0) or 0),
                title=_strip_highlight(str(r.get("title", ""))),
                author=str(r.get("author", "")),
                mid=int(r.get("mid", 0) or 0),
                pic=_normalize_pic(str(r.get("pic", ""))),
                description=str(r.get("description", "")),
                play=int(r.get("play", 0) or 0),
                danmaku=int(r.get("danmaku", 0) or 0),
                like=int(r.get("like", 0) or 0),
                favorites=int(r.get("favorites", 0) or 0),
                duration=str(r.get("duration", "")),
                pubdate=int(r.get("pubdate", 0) or 0),
                tag=str(r.get("tag", "")),
            )
        )
    return SearchResult(
        results=tuple(items),
        num_results=int(data.get("numResults", len(items))),
        page=int(data.get("page", page)),
        num_pages=int(data.get("numPages", 0)),
    )
