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
from ..subscription.delivery import MAX_ATTEMPTS, DeliveryJob
from ..subscription.manager import Subscription
from ._render_helper import render_note_components

try:
    from astrbot.api.event import MessageChain  # type: ignore[import]
    from astrbot.api.message_components import Plain  # type: ignore[import]
except Exception:  # pragma: no cover
    Plain = MessageChain = None  # type: ignore[assignment]


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
    latest = videos[0] if videos else None
    if latest is not None:
        targets = await manager.get_push_origins() or [origin]
        await manager.discover_video(
            origin, sub.mid, latest, targets, auto_summary=services.config.auto_push_summary
        )
    jobs = [
        job for job in await manager.get_delivery_jobs(origin, sub.mid)
        if job.status not in {"completed", "cancelled"}
    ]
    completed = 0
    for job in jobs:
        # Removing a target can settle the job without another send.
        if not job.basic_targets and not job.summary_targets:
            if await _save_progress(services, origin, sub, job):
                completed += int(job.status == "completed")

    now = time.time()
    new_job = next((
        job for job in jobs if latest is not None and job.bvid == latest.bvid
        and job.attempts == 0 and job.basic_due(now, force=force)
    ), None)
    if new_job is not None:
        completed += await _push_basic(services, origin, sub, new_job, force=force)

    # Give a new video its first notification before doing bounded compensation.
    basic_job = min(
        (job for job in jobs if job is not new_job and job.basic_due(now, force=force)),
        key=lambda job: job.next_retry_at or now,
        default=None,
    )
    if basic_job is not None:
        completed += await _push_basic(services, origin, sub, basic_job, force=force)

    now = time.time()
    if new_job is not None and new_job.summary_due(now, force=force):
        completed += await _push_summary(services, origin, sub, new_job, force=force)

    # Keep a compensation slot even when every pass discovers a fresh video.
    now = time.time()
    summary_job = min(
        (job for job in jobs if job is not new_job and job.summary_due(now, force=force)),
        key=lambda job: job.summary_next_retry_at or now,
        default=None,
    )
    if summary_job is not None:
        completed += await _push_summary(services, origin, sub, summary_job, force=force)
    return completed


async def _push_basic(
    services: BiliVideoServices, origin: str, sub: Subscription, job: DeliveryJob, *, force: bool
) -> int:
    if not await _refresh_targets(services, origin, sub, job):
        return 0
    if not job.basic_targets:
        return int(job.status == "completed")
    job.begin_attempt(time.time(), force=force)
    if not await _save_progress(services, origin, sub, job):
        return 0
    chain = _build_basic_chain(sub, LatestVideo(**job.video))
    if not await _deliver(services, origin, sub, job, chain, summary=False):
        return 0
    if job.basic_targets:
        await _notify_failure(services, origin, sub, job, summary=False)
    return int(job.status == "completed")


async def _push_summary(
    services: BiliVideoServices, origin: str, sub: Subscription, job: DeliveryJob, *, force: bool
) -> int:
    if not await _refresh_targets(services, origin, sub, job):
        return 0
    if not job.summary_targets:
        return int(job.status == "completed")
    job.begin_summary_attempt(time.time(), force=force)
    if not await _save_progress(services, origin, sub, job):
        return 0
    if not job.summary_targets:
        return int(job.status == "completed")

    if job.summary_status != "ready":
        try:
            note = await services.orchestrator.generate(f"https://www.bilibili.com/video/{job.bvid}")
            if not note.markdown.strip():
                raise ValueError("empty summary")
            video_info = asdict(note.video_info)
            video_info.pop("raw", None)
        except Exception as exc:
            job.summary_status = "failed"
            job.summary_error_kind = type(exc).__name__
            services.logger.warning(f"subscription summary generation failed: {type(exc).__name__}")
            if await _save_progress(services, origin, sub, job):
                await _notify_failure(services, origin, sub, job, summary=True)
            return int(job.status == "completed")

        job.markdown = note.markdown
        job.video_info = video_info
        job.summary_status = "ready"
        if not await _save_progress(services, origin, sub, job):
            return 0
        if not job.summary_targets:
            return int(job.status == "completed")

    try:
        chain = await _build_summary_chain(services, job)
    except Exception as exc:
        job.summary_error_kind = type(exc).__name__
        services.logger.warning(f"subscription summary rendering failed: {type(exc).__name__}")
        if await _save_progress(services, origin, sub, job):
            await _notify_failure(services, origin, sub, job, summary=True)
        return int(job.status == "completed")

    if not await _deliver(services, origin, sub, job, chain, summary=True):
        return 0
    if job.summary_targets:
        await _notify_failure(services, origin, sub, job, summary=True)
    return int(job.status == "completed")


async def _deliver(
    services: BiliVideoServices,
    origin: str,
    sub: Subscription,
    job: DeliveryJob,
    chain: list[Any],
    *,
    summary: bool,
) -> bool:
    targets = job.summary_targets if summary else job.basic_targets
    stage = "summary" if summary else "basic video"
    for target in targets:
        if not await _refresh_targets(services, origin, sub, job):
            return False
        if target in job.withdrawn:
            continue
        error = await _send_message(services, target, chain, stage)
        if error:
            if summary:
                job.summary_error_kind = error
            else:
                job.error_kind = error
            continue
        if summary:
            job.summary_delivered.append(target)
        else:
            job.record_basic_delivery(target)
        # Persist each receipt before the next send or expensive summary work.
        if not await _save_progress(services, origin, sub, job):
            return False
        services.logger.info(f"subscription {stage} delivery confirmed")
    return await _save_progress(services, origin, sub, job)


async def _send_message(
    services: BiliVideoServices, target: str, chain: list[Any], stage: str
) -> str:
    try:
        result = await services.astrbot_context.send_message(target, MessageChain(chain=chain))
    except Exception as exc:
        error = type(exc).__name__
    else:
        if result is True:
            return ""
        error = "SendNotConfirmed"
    services.logger.warning(f"subscription {stage} send failed: {error}")
    return error


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
    return await _save_progress(services, origin, sub, job)


async def _save_progress(
    services: BiliVideoServices, origin: str, sub: Subscription, job: DeliveryJob
) -> bool:
    job.update_status()
    saved = await services.subscription_manager.save_delivery_job(origin, sub.mid, job)
    return saved and job.status != "cancelled"


async def _notify_failure(
    services: BiliVideoServices, origin: str, sub: Subscription, job: DeliveryJob, *, summary: bool
) -> None:
    if summary:
        targets, notified, attempts = job.summary_targets, job.summary_notified, job.summary_attempts
        failure = "视频总结投递未确认" if job.summary_status == "ready" else "视频总结生成失败"
    else:
        targets, notified, attempts = job.basic_targets, job.notified, job.attempts
        failure = "新视频通知投递未确认"
    state = (
        "自动重试次数已用完。" if attempts >= MAX_ATTEMPTS else "任务已保留，将自动重试。"
    )
    title = job.video.get("title", job.bvid)
    chain = [Plain(f"⚠️ 《{title}》{failure}。{state}可在订阅会话发送 /检查更新 重试。")]
    for target in targets:
        if not await _refresh_targets(services, origin, sub, job):
            return
        if target in notified or target in job.withdrawn:
            continue
        if await _send_message(services, target, chain, "failure notice"):
            continue
        notified.append(target)
        if not await _save_progress(services, origin, sub, job):
            return


# ──────────────────────────── helpers ──────────────────────────────


def _build_basic_chain(sub: Subscription, latest: LatestVideo) -> list[Any]:
    push_header = f"🔔 UP主【{sub.name}】发布了新视频!\n"
    lines = [push_header + f"📺 {latest.title}"]
    if latest.description:
        desc = latest.description if len(latest.description) <= 100 else latest.description[:100] + "..."
        lines.append(f"📝 简介: {desc}")
    if latest.pubdate:
        with contextlib.suppress(ValueError, OSError, OverflowError):
            lines.append(f"📅 发布: {time.strftime('%Y-%m-%d %H:%M', time.localtime(latest.pubdate))}")
    lines.append(f"🔗 https://www.bilibili.com/video/{latest.bvid}")
    return [Plain("\n".join(lines))]


async def _build_summary_chain(services: BiliVideoServices, job: DeliveryJob) -> list[Any]:
    config = services.config
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
            )
            return [forward]
        except Exception as exc:
            services.logger.warning(f"forward fallback: {type(exc).__name__}")

    heading = f"📝 《{job.video.get('title', job.bvid)}》的 AI 视频总结"
    if isinstance(rendered, list):
        return [Plain(heading), *rendered]
    return [Plain(f"{heading}\n\n{rendered}")]
