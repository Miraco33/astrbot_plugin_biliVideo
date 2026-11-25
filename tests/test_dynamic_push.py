"""Exercise dynamic delivery without a live Bilibili or AstrBot connection."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bilivideo.access.keyed_lock import KeyedLocks
from bilivideo.api.endpoints import LatestDynamic
from bilivideo.core.exceptions import NetworkError
from bilivideo.handlers import dynamic_push as push
from bilivideo.handlers import subscription as commands
from bilivideo.subscription.manager import SubscriptionManager

ORIGIN = "mock-origin"
MID = "mock-uploader"
TARGET_A = "mock-target-a"
TARGET_B = "mock-target-b"


def dynamic(dynamic_id: int = 100) -> LatestDynamic:
    return LatestDynamic(
        dynamic_id=str(dynamic_id),
        pubdate=int(time.time()) + 10,
        text="动态正文" * 350,
        url=f"https://t.bilibili.com/{dynamic_id}",
        kind="DYNAMIC_TYPE_WORD",
    )


@pytest.fixture
def services(tmp_path, monkeypatch):
    monkeypatch.setattr(push, "MessageChain", lambda **kwargs: kwargs)
    monkeypatch.setattr(push, "Plain", lambda value: value)
    monkeypatch.setattr(push, "get_latest_dynamics", AsyncMock(return_value=[dynamic()]))
    return SimpleNamespace(
        subscription_manager=SubscriptionManager(str(tmp_path)),
        dynamic_push_locks=KeyedLocks(),
        astrbot_context=SimpleNamespace(send_message=AsyncMock(return_value=True)),
        http_client=object(),
        logger=MagicMock(),
    )


async def subscribe(services):
    manager = services.subscription_manager
    await manager.add_subscription(ORIGIN, MID, "测试 UP 主")
    return await manager.get_subscription(ORIGIN, MID)


@pytest.mark.asyncio
async def test_partial_dynamic_delivery_resumes_without_resending_receipt(services, tmp_path):
    sub = await subscribe(services)
    manager = services.subscription_manager
    for target in (TARGET_A, TARGET_B):
        await manager.add_push_target(target)
    send = services.astrbot_context.send_message
    send.side_effect = [True, 1]  # A truthy value is insufficient to confirm delivery.

    assert await push.dynamic_push_callback(services, ORIGIN, sub) == 0
    (job,) = await manager.get_dynamic_jobs(ORIGIN, MID)
    assert job.delivered == [TARGET_A]
    assert job.status == "retry"
    body = send.await_args_list[0].args[1]["chain"][0]
    heading, text, url = body.splitlines()
    assert heading == "🔔 UP主【测试 UP 主】发布了新动态"
    assert len(text) == push.MAX_TEXT_LENGTH and text.endswith("…")
    assert url == dynamic().url
    assert await push.dynamic_push_callback(services, ORIGIN, sub) == 0
    assert send.await_count == 2

    manager.close()
    services.subscription_manager = SubscriptionManager(str(tmp_path))
    send.reset_mock(side_effect=True)
    push.get_latest_dynamics.side_effect = NetworkError("synthetic discovery failure")
    result = await push.check_dynamics(services, ORIGIN, sub, force=True)
    assert result == push.DynamicCheckResult(delivered=1, discovery_failed=True)
    assert send.await_count == 1 and send.await_args.args[0] == TARGET_B
    (job,) = await services.subscription_manager.get_dynamic_jobs(ORIGIN, MID)
    assert job.status == "completed" and job.delivered == [TARGET_A, TARGET_B]


@pytest.mark.asyncio
async def test_withdrawn_target_is_skipped_during_dynamic_delivery(services):
    sub = await subscribe(services)
    manager = services.subscription_manager
    for target in (TARGET_A, TARGET_B):
        await manager.add_push_target(target)

    async def send_message(target, _message):
        assert target == TARGET_A
        await manager.remove_push_target(TARGET_B)
        return True

    services.astrbot_context.send_message.side_effect = send_message
    assert await push.dynamic_push_callback(services, ORIGIN, sub) == 1
    (job,) = await manager.get_dynamic_jobs(ORIGIN, MID)
    assert job.delivered == [TARGET_A] and job.withdrawn == [TARGET_B]
    assert services.astrbot_context.send_message.await_count == 1


@pytest.mark.asyncio
async def test_manual_and_scheduled_dynamic_checks_share_receipts(services):
    sub = await subscribe(services)
    scheduled, manual = await asyncio.gather(
        push.dynamic_push_callback(services, ORIGIN, sub),
        push.check_dynamics(services, ORIGIN, sub, force=True),
    )
    assert scheduled + manual.delivered == 1
    assert services.astrbot_context.send_message.await_count == 1


@pytest.mark.asyncio
async def test_manual_dynamic_notification_completes_while_video_worker_waits(services, monkeypatch):
    await subscribe(services)
    await services.subscription_manager.add_subscription(ORIGIN, "mock-second-uploader", "第二个 UP 主")
    push.get_latest_dynamics.side_effect = [[dynamic(100)], [dynamic(200)]]
    delivered = asyncio.Event()
    release_video = asyncio.Event()

    async def wait_for_video(*_args, **_kwargs):
        await release_video.wait()
        return 0

    async def send_message(_target, message):
        if message["chain"][0].endswith(dynamic(200).url):
            delivered.set()
        return True

    services.astrbot_context.send_message.side_effect = send_message
    monkeypatch.setattr(commands, "push_callback", wait_for_video)
    monkeypatch.setattr(commands.asyncio, "sleep", AsyncMock())
    event = SimpleNamespace(unified_msg_origin=ORIGIN, plain_result=lambda value: value)

    async def check():
        return [reply async for reply in commands.handle_check_updates.__wrapped__(services, event)]

    task = asyncio.create_task(check())
    try:
        await asyncio.wait_for(delivered.wait(), timeout=2)
        assert not task.done()
    finally:
        release_video.set()
        replies = await asyncio.wait_for(task, timeout=2)
    assert "视频 0 个，动态 2 个" in replies[-1]
