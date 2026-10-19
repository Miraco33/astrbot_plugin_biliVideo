"""Subscription-related command handlers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from ..api.endpoints import (
    get_latest_videos,
    get_uploader_info,
    get_video_info,
    search_uploader_by_name,
)
from ..core.exceptions import BiliVideoError
from ..parsing.url_extractor import extract_uid
from ..services import BiliVideoServices
from .scheduled_push import push_callback
from ._utils import parse_command_args, require_access

_SAVE_FAILED_MESSAGE = "❌ 订阅数据保存失败,请稍后重试"


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
        yield event.plain_result(  # type: ignore[attr-defined]
            f"✅ 已订阅 UP主【{name}】(UID:{mid})\n有新视频时将自动推送总结"
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
        f"🔍 正在检查 {len(subs)} 个UP主；未完成任务将重试，总结按推送目标发送。"
    )
    completed = 0
    pending = 0
    exhausted = 0
    for sub in subs:
        try:
            completed += await push_callback(services, origin, sub, force=True)
            jobs = await services.subscription_manager.get_delivery_jobs(origin, sub.mid)
            pending += sum(job.status not in {"completed", "exhausted"} for job in jobs)
            exhausted += sum(job.status == "exhausted" for job in jobs)
        except (BiliVideoError, OSError) as exc:
            services.logger.warning(f"manual subscription check failed: {type(exc).__name__}")
            pending += 1
        await asyncio.sleep(2)
    yield event.plain_result(  # type: ignore[attr-defined]
        f"✅ 检查完成：本次确认完成投递 {completed} 个；"
        f"待重试 {pending} 个，自动重试耗尽 {exhausted} 个。"
    )
