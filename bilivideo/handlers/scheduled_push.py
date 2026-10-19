"""Subscription delivery with durable, bounded compensation and per-target receipts."""

from __future__ import annotations

import contextlib
import time
from dataclasses import asdict
from typing import Any

from ..api.endpoints import get_latest_videos
from ..core.exceptions import BiliVideoError
from ..core.types import LatestVideo, VideoInfo
from ..messaging.forward import build_video_forward_nodes
from ..services import BiliVideoServices
from ..subscription.delivery import DeliveryJob
from ..subscription.manager import Subscription
from ._render_helper import render_note_components

try:
    from astrbot.api.event import MessageChain  # type: ignore[import]
    from astrbot.api.message_components import Image, Plain  # type: ignore[import]
except Exception:  # pragma: no cover
    Plain = Image = MessageChain = None  # type: ignore[assignment]


async def push_callback(
    services: BiliVideoServices,
    origin: str,
    sub: Subscription,
    *,
    force: bool = False,
) -> int:
    if services.astrbot_context is None or MessageChain is None or Plain is None:
        services.logger.error("subscription delivery unavailable")
        return 0
    # Manual checks and the scheduler share the lock AND the durable state.
    async with services.push_locks.acquire((origin, sub.mid)):
        fresh = await services.subscription_manager.get_subscription(origin, sub.mid)
        if fresh is None:
            return 0
        return await _push_if_new(services, origin, fresh, force=force)


async def _push_if_new(
    services: BiliVideoServices,
    origin: str,
    sub: Subscription,
    *,
    force: bool = False,
) -> int:
    manager = services.subscription_manager
    try:
        videos = await get_latest_videos(services.http_client, sub.mid, count=1)
    except BiliVideoError as exc:
        services.logger.warning(f"subscription discovery failed: {type(exc).__name__}")
        videos = []
    if videos:
        targets = await manager.get_push_origins() or [origin]
        await manager.discover_video(
            origin, sub.mid, videos[0], targets, auto_summary=services.config.auto_push_summary
        )
    # Recover existing jobs even if discovery fails or a newer video exists.
    # One due job per subscription per pass bounds compensation work.
    now = time.time()
    jobs = await manager.get_delivery_jobs(origin, sub.mid)
    job = next((item for item in jobs if item.due(now, force=force)), None)
    if job is None:
        return 0
    if not await _refresh_targets(services, origin, sub, job):
        return 0
    job.begin_attempt(now, force=force)
    await manager.save_delivery_job(origin, sub.mid, job)

    if job.auto_summary and job.summary_status != "ready":
        try:
            note = await services.orchestrator.generate(
                f"https://www.bilibili.com/video/{job.bvid}"
            )
            job.markdown = note.markdown
            job.video_info = asdict(note.video_info)
            job.video_info.pop("raw", None)
            job.summary_status = "ready"
            await manager.save_delivery_job(origin, sub.mid, job)
        except Exception as exc:
            job.summary_status = "failed"
            job.fail(type(exc).__name__)
            await manager.save_delivery_job(origin, sub.mid, job)
            services.logger.warning(f"subscription summary failed: {type(exc).__name__}")
            await _notify_failure(services, origin, sub, job)
            return 0

    if not await _refresh_targets(services, origin, sub, job):
        return 0
    try:
        chain = await _build_chain(services, sub, LatestVideo(**job.video), job)
    except Exception as exc:
        job.fail(type(exc).__name__)
        await manager.save_delivery_job(origin, sub.mid, job)
        services.logger.warning(f"subscription rendering failed: {type(exc).__name__}")
        await _notify_failure(services, origin, sub, job)
        return 0
    if not chain:
        job.fail("EmptyMessage")
        await manager.save_delivery_job(origin, sub.mid, job)
        return 0

    for target in job.targets:
        if not await _refresh_targets(services, origin, sub, job):
            return 0
        if target in job.delivered or target in job.withdrawn:
            continue
        try:
            result = await services.astrbot_context.send_message(
                target, MessageChain(chain=chain)
            )
        except Exception as exc:
            services.logger.warning(f"subscription send failed: {type(exc).__name__}")
            job.error_kind = type(exc).__name__
        else:
            if result is True:
                job.delivered.append(target)
                # Save each positive receipt before sending to another target.
                await manager.save_delivery_job(origin, sub.mid, job)
            else:
                job.error_kind = "SendNotConfirmed"
    if job.all_delivered:
        job.status = "completed"
        job.error_kind = ""
        job.next_retry_at = 0
        await manager.save_delivery_job(origin, sub.mid, job)
        return 1
    job.fail(job.error_kind or "SendNotConfirmed")
    await manager.save_delivery_job(origin, sub.mid, job)
    await _notify_failure(services, origin, sub, job)
    return 0


async def _refresh_targets(
    services: BiliVideoServices, origin: str, sub: Subscription, job: DeliveryJob
) -> bool:
    manager = services.subscription_manager
    if await manager.get_subscription(origin, sub.mid) is None:
        return False
    current = await manager.get_push_origins() or [origin]
    for target in job.targets:
        if target not in current and target not in job.withdrawn:
            job.withdrawn.append(target)
    if all(target in job.withdrawn for target in job.targets):
        job.status = "cancelled"
        job.error_kind = "TargetsRemoved"
        await manager.save_delivery_job(origin, sub.mid, job)
        return False
    await manager.save_delivery_job(origin, sub.mid, job)
    return True


async def _notify_failure(
    services: BiliVideoServices, origin: str, sub: Subscription, job: DeliveryJob
) -> None:
    # Never surface untrusted exception messages or signed media URLs.
    state = "自动重试预算已用完，可发送 /检查更新 重试。" if job.status == "exhausted" else (
        "任务已保留，将退避重试；也可发送 /检查更新 重试。"
    )
    for target in job.targets:
        if not await _refresh_targets(services, origin, sub, job):
            return
        if target in job.notified or target in job.delivered or target in job.withdrawn:
            continue
        try:
            result = await services.astrbot_context.send_message(
                target, MessageChain(chain=[Plain(f"⚠️ 视频处理或投递未完成。{state}")])
            )
        except Exception as exc:
            services.logger.warning(f"subscription notice failed: {type(exc).__name__}")
            continue
        if result is True:
            job.notified.append(target)
            await services.subscription_manager.save_delivery_job(origin, sub.mid, job)


# ──────────────────────────── helpers ──────────────────────────────


async def _build_chain(
    services: BiliVideoServices,
    sub: Subscription,
    latest: LatestVideo,
    job: DeliveryJob,
) -> list[Any]:
    push_header = f"🔔 UP主【{sub.name}】发布了新视频!\n"
    config = services.config
    if Plain is None:  # AstrBot message components unavailable; cannot build a chain
        services.logger.error("scheduled push: Plain component unavailable; skipping")
        return []
    bvid = latest.bvid
    video_url = f"https://www.bilibili.com/video/{bvid}"
    info: VideoInfo | None = None

    if not job.auto_summary:
        # basic info only
        lines = [push_header + f"📺 {latest.title}"]
        if latest.description:
            desc = latest.description if len(latest.description) <= 100 else latest.description[:100] + "..."
            lines.append(f"📝 简介: {desc}")
        if latest.pubdate:
            with contextlib.suppress(ValueError, OSError):
                lines.append(
                    f"📅 发布: {time.strftime('%Y-%m-%d %H:%M', time.localtime(latest.pubdate))}"
                )
        lines.append(f"🔗 {video_url}")
        chain: list[Any] = []
        if latest.pic and Image is not None:
            chain.append(Image.fromURL(latest.pic))
        chain.append(Plain("\n".join(lines)))
        return chain

    # Persisted text avoids another ASR/model call after partial delivery.
    info = VideoInfo(**job.video_info) if job.video_info else None
    rendered = await render_note_components(services, job.markdown)

    if config.enable_forward_message and info is not None:
        try:
            forward = build_video_forward_nodes(
                info,
                rendered,
                bot_name=config.forward_bot_name,
                bot_uin=config.forward_bot_uin,
                header=push_header.rstrip(),
            )
            return [forward]
        except Exception as exc:
            services.logger.warning(f"forward fallback: {type(exc).__name__}")

    if isinstance(rendered, list):
        return [Plain(push_header), *list(rendered)]
    if info is not None:
        # fall back to header + info+rendered
        body = f"{push_header}━━━━━━━━━━━━━━━━━━━\n\n{rendered}"
        return [Plain(body)]
    return [Plain(push_header + str(rendered))]
