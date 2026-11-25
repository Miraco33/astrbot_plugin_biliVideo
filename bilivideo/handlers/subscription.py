"""Subscription-related command handlers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

from ..api.endpoints import (
    get_latest_videos,
    get_uploader_info,
    get_video_info,
    search_uploader_by_name,
)
from ..core.exceptions import BiliVideoError
from ..parsing.url_extractor import extract_uid
from ..services import BiliVideoServices
from ..subscription.manager import Subscription
from ._utils import parse_command_args, require_access
from .dynamic_push import DynamicCheckResult, check_dynamics
from .scheduled_push import push_callback

_SAVE_FAILED_MESSAGE = "❌ 订阅数据保存失败,请稍后重试"


@dataclass(slots=True)
class _ManualCheckCounts:
    delivered: int = 0
    pending: int = 0
    exhausted: int = 0
    failed: int = 0


@require_access
async def handle_subscribe(services: BiliVideoServices, event: object) -> AsyncIterator[object]:
    args = parse_command_args(getattr(event, "message_str", "") or "")
    if not args:
        yield event.plain_result(  # type: ignore[attr-defined]
            "❌ 请提供UP主UID、空间链接或昵称\n用法: /订阅 <UP主UID或昵称>"
        )
        return

    name = ""
    mid = extract_uid(args)
    if not mid:
        yield event.plain_result(f"🔍 正在搜索UP主: {args}...")  # type: ignore[attr-defined]
        uploader = await search_uploader_by_name(services.http_client, args)
        if uploader is None:
            yield event.plain_result(  # type: ignore[attr-defined]
                "❌ 无法识别UP主\n支持: 纯数字UID、空间链接、或UP主昵称"
            )
            return
        mid = uploader.mid
        name = uploader.name
        yield event.plain_result(f"✅ 找到UP主【{name}】(UID:{mid})")  # type: ignore[attr-defined]

    origin = getattr(event, "unified_msg_origin", "")
    count = await services.subscription_manager.get_subscription_count(origin)
    if count >= services.config.max_subscriptions:
        yield event.plain_result(f"❌ 已达到最大订阅数 ({services.config.max_subscriptions})")  # type: ignore[attr-defined]
        return

    if not name:
        info = await get_uploader_info(services.http_client, mid)
        if info is not None:
            name = info.name
        else:
            videos = await get_latest_videos(services.http_client, mid, count=1)
            if videos:
                try:
                    video_info = await get_video_info(services.http_client, videos[0].bvid)
                    if video_info.owner_name:
                        name = video_info.owner_name
                except BiliVideoError as exc:
                    services.logger.debug(f"owner name lookup failed for {mid}: {exc}")
            if not name:
                name = f"UP主_{mid}"

    try:
        added = await services.subscription_manager.add_subscription(origin, mid, name)
        if added:
            videos = await get_latest_videos(services.http_client, mid, count=1)
            if videos:
                await services.subscription_manager.update_last_video(origin, mid, videos[0].bvid)
    except OSError as exc:
        services.logger.error(f"subscribe save failed for {mid}: {exc}")
        yield event.plain_result(_SAVE_FAILED_MESSAGE)  # type: ignore[attr-defined]
        return
    if added:
        mode = "已开启自动推送" if services.config.enable_auto_push else "发送 /检查更新 手动检查"
        yield event.plain_result(  # type: ignore[attr-defined]
            f"✅ 已订阅 UP主【{name}】(UID:{mid}) 的新视频和动态。{mode}。"
        )
    else:
        yield event.plain_result(f"⚠️ 已经订阅了 UP主【{name}】(UID:{mid})")  # type: ignore[attr-defined]


@require_access
async def handle_unsubscribe(services: BiliVideoServices, event: object) -> AsyncIterator[object]:
    args = parse_command_args(getattr(event, "message_str", "") or "")
    if not args:
        yield event.plain_result(  # type: ignore[attr-defined]
            "❌ 请提供UP主UID、空间链接或昵称\n用法: /取消订阅 <UP主UID或昵称>"
        )
        return

    mid = extract_uid(args)
    if not mid:
        yield event.plain_result(f"🔍 正在搜索UP主: {args}...")  # type: ignore[attr-defined]
        uploader = await search_uploader_by_name(services.http_client, args)
        if uploader is None:
            yield event.plain_result(  # type: ignore[attr-defined]
                "❌ 无法识别UP主\n支持: 纯数字UID、空间链接、或UP主昵称"
            )
            return
        mid = uploader.mid

    origin = getattr(event, "unified_msg_origin", "")
    try:
        removed = await services.subscription_manager.remove_subscription(origin, mid)
    except OSError as exc:
        services.logger.error(f"unsubscribe save failed for {mid}: {exc}")
        yield event.plain_result(_SAVE_FAILED_MESSAGE)  # type: ignore[attr-defined]
        return
    if removed:
        yield event.plain_result(f"✅ 已取消订阅 (UID:{mid})")  # type: ignore[attr-defined]
    else:
        yield event.plain_result(f"⚠️ 未找到该订阅 (UID:{mid})")  # type: ignore[attr-defined]


@require_access
async def handle_list_subscriptions(
    services: BiliVideoServices, event: object
) -> AsyncIterator[object]:
    origin = getattr(event, "unified_msg_origin", "")
    subs = await services.subscription_manager.get_subscriptions(origin)
    if not subs:
        yield event.plain_result(  # type: ignore[attr-defined]
            "📋 当前没有订阅任何UP主\n使用 /订阅 <UID或昵称> 添加订阅"
        )
        return

    lines = ["📋 当前订阅列表:", "━━━━━━━━━━━━━━━━━━━"]
    for i, up in enumerate(subs, start=1):
        lines.append(f"  {i}. {up.name} (UID:{up.mid})")
    lines.append(f"\n共 {len(subs)} 个订阅")
    yield event.plain_result("\n".join(lines))  # type: ignore[attr-defined]


@require_access
async def handle_check_updates(
    services: BiliVideoServices, event: object
) -> AsyncIterator[object]:
    origin = getattr(event, "unified_msg_origin", "")
    subs = await services.subscription_manager.get_subscriptions(origin)
    if not subs:
        yield event.plain_result("📋 当前没有订阅任何UP主,无法检查更新")  # type: ignore[attr-defined]
        return
    yield event.plain_result(  # type: ignore[attr-defined]
        f"🔍 正在检查 {len(subs)} 个UP主的新视频和动态，并重试未完成的投递。"
    )
    video, dynamic = await asyncio.gather(
        _check_subscriptions(services, origin, subs, dynamic=False),
        _check_subscriptions(services, origin, subs, dynamic=True),
    )
    delivered = video.delivered + dynamic.delivered
    pending = video.pending + dynamic.pending
    exhausted = video.exhausted + dynamic.exhausted
    failed = video.failed + dynamic.failed
    issues = f"另有 {failed} 项检查未完成。" if failed else ""
    yield event.plain_result(  # type: ignore[attr-defined]
        f"✅ 检查结束。本次确认完成投递 {delivered} 个"
        f"（视频 {video.delivered} 个，动态 {dynamic.delivered} 个）。"
        f"待重试 {pending} 个，自动重试耗尽 {exhausted} 个。{issues}"
    )


async def _check_subscriptions(
    services: BiliVideoServices, origin: str, subs: list[Subscription], *, dynamic: bool,
) -> _ManualCheckCounts:
    counts = _ManualCheckCounts()
    manager = services.subscription_manager
    check = check_dynamics if dynamic else push_callback
    get_jobs = manager.get_dynamic_jobs if dynamic else manager.get_delivery_jobs
    source = "dynamic" if dynamic else "video"
    for index, sub in enumerate(subs):
        if index:
            await asyncio.sleep(2)
        try:
            result = await check(services, origin, sub, force=True)
        except Exception as exc:
            services.logger.warning(f"manual {source} subscription check failed: {type(exc).__name__}")
            counts.failed += 1
        else:
            if isinstance(result, DynamicCheckResult):
                counts.delivered += result.delivered
                counts.failed += result.discovery_failed
            else:
                counts.delivered += result
        try:
            jobs = await get_jobs(origin, sub.mid)
        except OSError as exc:
            services.logger.warning(f"manual {source} queue read failed: {type(exc).__name__}")
            counts.failed += 1
        else:
            counts.pending += sum(job.status in {"pending", "retry"} for job in jobs)
            counts.exhausted += sum(job.status == "exhausted" for job in jobs)
    return counts
