"""Discover public space dynamics and deliver durable plain-text notifications."""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..api.endpoints import get_latest_dynamics
from ..core.exceptions import BiliVideoError
from ..services import BiliVideoServices
from ..subscription.dynamic_delivery import DynamicDeliveryJob
from ..subscription.manager import Subscription

try:
    from astrbot.api.event import MessageChain  # type: ignore[import]
    from astrbot.api.message_components import Plain  # type: ignore[import]
except ImportError:  # pragma: no cover - offline test environment
    Plain = MessageChain = None  # type: ignore[assignment]

MAX_JOBS_PER_CHECK = 5
MAX_TEXT_LENGTH = 500


@dataclass(frozen=True, slots=True)
class DynamicCheckResult:
    delivered: int = 0
    discovery_failed: bool = False


async def dynamic_push_callback(
    services: BiliVideoServices, origin: str, sub: Subscription
) -> int:
    return (await check_dynamics(services, origin, sub)).delivered


async def check_dynamics(
    services: BiliVideoServices, origin: str, sub: Subscription, *, force: bool = False
) -> DynamicCheckResult:
    manager = services.subscription_manager
    async with services.dynamic_push_locks.acquire((origin, sub.mid)):
        fresh = await manager.get_subscription(origin, sub.mid)
        if fresh is None:
            return DynamicCheckResult()
        discovered = await _discover_dynamics(services, origin, sub.mid)
        if services.astrbot_context is None or MessageChain is None or Plain is None:
            services.logger.error("dynamic delivery unavailable")
            return DynamicCheckResult(discovery_failed=not discovered)

        now = time.time()
        jobs = await manager.get_dynamic_jobs(origin, sub.mid)
        due = [
            job for job in jobs
            if job.due(now, force=force)
            or (job.status in {"pending", "retry", "exhausted"} and job.all_delivered)
        ]
        delivered = 0
        for job in due[:MAX_JOBS_PER_CHECK]:
            delivered += await _deliver_dynamic(services, origin, fresh, job, now, force=force)
        if delivered:
            services.logger.info(f"dynamic delivery confirmed: {delivered}")
        return DynamicCheckResult(delivered=delivered, discovery_failed=not discovered)


async def _discover_dynamics(services: BiliVideoServices, origin: str, mid: str) -> bool:
    try:
        dynamics = await get_latest_dynamics(services.http_client, mid)
    except (BiliVideoError, UnicodeDecodeError) as exc:
        services.logger.warning(f"dynamic discovery failed: {type(exc).__name__}")
        return False
    manager = services.subscription_manager
    targets = await manager.get_push_origins() or [origin]
    await manager.discover_dynamics(origin, mid, dynamics, targets)
    services.logger.info(f"dynamic discovery succeeded: {len(dynamics)} items")
    return True


async def _deliver_dynamic(
    services: BiliVideoServices,
    origin: str,
    sub: Subscription,
    job: DynamicDeliveryJob,
    now: float,
    *,
    force: bool,
) -> bool:
    manager = services.subscription_manager
    if not await _refresh_targets(services, origin, sub.mid, job):
        return False
    if job.all_delivered:
        return await _complete_dynamic(services, origin, sub.mid, job)
    if not job.due(now, force=force):
        return False
    job.begin_attempt(now, force=force)
    if not await manager.save_dynamic_job(origin, sub.mid, job) or job.status == "cancelled":
        return False

    text = _dynamic_text(sub, job)
    for target in job.targets:
        if target in job.delivered or target in job.withdrawn:
            continue
        if not await _refresh_targets(services, origin, sub.mid, job):
            return False
        if target in job.withdrawn:
            continue
        try:
            result = await services.astrbot_context.send_message(
                target, MessageChain(chain=[Plain(text)])
            )
        except Exception as exc:
            job.error_kind = type(exc).__name__
            services.logger.warning(f"dynamic send failed: {job.error_kind}")
        else:
            if result is not True:
                job.error_kind = "SendNotConfirmed"
                continue
            job.delivered.append(target)
            if job.all_delivered:
                return await _complete_dynamic(services, origin, sub.mid, job)
            # Persist each confirmed target before attempting the next send.
            if not await manager.save_dynamic_job(origin, sub.mid, job) or job.status == "cancelled":
                return False

    if job.all_delivered:
        return await _complete_dynamic(services, origin, sub.mid, job)
    job.fail(job.error_kind or "SendNotConfirmed")
    if await manager.save_dynamic_job(origin, sub.mid, job) and job.status != "cancelled":
        services.logger.warning(f"dynamic delivery incomplete ({job.status}): {job.error_kind}")
    return False


async def _refresh_targets(
    services: BiliVideoServices, origin: str, mid: str, job: DynamicDeliveryJob
) -> bool:
    manager = services.subscription_manager
    current = await manager.get_push_origins() or [origin]
    for target in job.targets:
        if target not in current and target not in job.withdrawn:
            job.withdrawn.append(target)
    # Saving merges durable withdrawals and refuses to revive a removed subscription.
    return await manager.save_dynamic_job(origin, mid, job) and job.status != "cancelled"


async def _complete_dynamic(
    services: BiliVideoServices, origin: str, mid: str, job: DynamicDeliveryJob
) -> bool:
    job.status = "completed"
    job.error_kind = ""
    job.next_retry_at = 0
    saved = await services.subscription_manager.save_dynamic_job(origin, mid, job)
    return saved and job.status == "completed"


def _dynamic_text(sub: Subscription, job: DynamicDeliveryJob) -> str:
    name = " ".join(sub.name.split())[:80]
    text = job.text.strip()
    if len(text) > MAX_TEXT_LENGTH:
        text = text[:MAX_TEXT_LENGTH - 1].rstrip() + "…"
    return "\n".join(filter(None, [f"🔔 UP主【{name}】发布了新动态", text, job.url]))
