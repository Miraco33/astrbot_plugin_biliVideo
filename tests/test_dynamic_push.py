"""Exercise dynamic delivery without a live Bilibili or AstrBot connection."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bilivideo.access.keyed_lock import KeyedLocks
from bilivideo.api.dynamic_images import DynamicImageDownloadError
from bilivideo.api.endpoints import LatestDynamic
from bilivideo.core.exceptions import NetworkError, RiskControlError
from bilivideo.handlers import dynamic_push as push
from bilivideo.handlers import subscription as commands
from bilivideo.subscription.dynamic_delivery import DynamicDeliveryJob
from bilivideo.subscription.manager import Subscription, SubscriptionManager

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


def test_dynamic_notice_displays_published_time_in_beijing() -> None:
    sub = Subscription(mid=MID, name="测试 UP 主")
    job = DynamicDeliveryJob(
        dynamic_id="100", text="动态正文", url="https://t.bilibili.com/100",
        pubdate=1704038400, targets=[ORIGIN],
    )

    assert push._dynamic_text(sub, job).splitlines()[1:] == [
        "北京时间 2024-01-01 00:00", "动态正文", job.url,
    ]


@pytest.fixture
def services(tmp_path, monkeypatch):
    monkeypatch.setattr(push, "MessageChain", lambda **kwargs: kwargs)
    monkeypatch.setattr(push, "Plain", lambda value: value)
    monkeypatch.setattr(push, "get_latest_dynamics", AsyncMock(return_value=[dynamic()]))
    return SimpleNamespace(
        subscription_manager=SubscriptionManager(str(tmp_path)),
        dynamic_push_locks=KeyedLocks(),
        dynamic_discovery_lock=asyncio.Lock(),
        dynamic_discovery_backoff={},
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
    heading, time_line, text, url = body.splitlines()
    assert heading == "🔔 UP主【测试 UP 主】发布了新动态"
    assert time_line.startswith("北京时间 ")
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
async def test_onebot_image_dynamic_sends_a_single_forward_and_persists_receipt(services, monkeypatch):
    sub = await subscribe(services)
    url = "https://i0.hdslb.com/bfs/new_dyn/photo.png"
    item = replace(dynamic(), image_urls=(url,))
    push.get_latest_dynamics.return_value = [item]
    platform = SimpleNamespace(meta=lambda: SimpleNamespace(name="aiocqhttp"))
    lookup = services.astrbot_context.get_platform_inst = MagicMock(return_value=platform)
    services.config = SimpleNamespace(forward_bot_name="Mock Bot", forward_bot_uin="0")
    download = AsyncMock(return_value=[b"sample"])
    build = MagicMock(return_value=object())
    monkeypatch.setattr(push, "download_dynamic_images", download)
    monkeypatch.setattr(push, "build_dynamic_forward_nodes", build)

    assert await push.dynamic_push_callback(services, ORIGIN, sub) == 1

    lookup.assert_called_once_with(ORIGIN)
    download.assert_awaited_once_with([url])
    text, images = build.call_args.args
    assert "北京时间 " in text and text.endswith(item.url)
    assert images == [b"sample"]
    services.astrbot_context.send_message.assert_awaited_once_with(ORIGIN, {"chain": [build.return_value]})
    (job,) = await services.subscription_manager.get_dynamic_jobs(ORIGIN, MID)
    assert job.image_urls == [url]
    assert job.status == "completed" and job.delivered == [ORIGIN]


@pytest.mark.asyncio
async def test_older_pending_dynamic_gains_images_and_retries_after_download_failure(services, monkeypatch):
    sub = await subscribe(services)
    item = dynamic()
    push.get_latest_dynamics.return_value = [item]
    send = services.astrbot_context.send_message
    send.return_value = False
    assert await push.dynamic_push_callback(services, ORIGIN, sub) == 0

    image_url = "https://i0.hdslb.com/bfs/album/photo.png"
    push.get_latest_dynamics.return_value = [replace(item, image_urls=(image_url,))]
    services.astrbot_context.get_platform_inst = lambda _id: SimpleNamespace(
        meta=lambda: SimpleNamespace(name="aiocqhttp")
    )
    services.config = SimpleNamespace(forward_bot_name="Mock Bot", forward_bot_uin="0")
    download = AsyncMock(side_effect=DynamicImageDownloadError("http_status"))
    monkeypatch.setattr(push, "download_dynamic_images", download)
    send.reset_mock()
    send.return_value = True

    assert (await push.check_dynamics(services, ORIGIN, sub, force=True)).delivered == 0

    send.assert_not_awaited()
    (job,) = await services.subscription_manager.get_dynamic_jobs(ORIGIN, MID)
    assert job.image_urls == [image_url] and job.status == "retry" and job.delivered == []
    assert job.error_kind == "DynamicImageDownloadError"


@pytest.mark.asyncio
async def test_image_dynamic_on_other_adapter_preserves_plain_notification(services, monkeypatch):
    sub = await subscribe(services)
    item = replace(dynamic(), image_urls=("https://i0.hdslb.com/bfs/new_dyn/photo.png",))
    push.get_latest_dynamics.return_value = [item]
    services.astrbot_context.get_platform_inst = lambda _id: SimpleNamespace(
        meta=lambda: SimpleNamespace(name="telegram")
    )
    download = AsyncMock()
    monkeypatch.setattr(push, "download_dynamic_images", download)

    assert await push.dynamic_push_callback(services, ORIGIN, sub) == 1

    download.assert_not_awaited()
    (text,) = services.astrbot_context.send_message.await_args.args[1]["chain"]
    assert text.endswith(item.url)


@pytest.mark.asyncio
async def test_dynamic_backlog_is_sent_in_order_with_spacing(services, monkeypatch):
    sub = await subscribe(services)
    first = dynamic(100)
    later = replace(dynamic(101), pubdate=first.pubdate + 1)
    push.get_latest_dynamics.return_value = [later, first]
    calls = []

    async def send(_target, message):
        calls.append(message["chain"][0].splitlines()[-1])
        return True

    async def pause(seconds):
        calls.append(seconds)

    services.astrbot_context.send_message.side_effect = send
    monkeypatch.setattr(push, "asyncio", SimpleNamespace(sleep=pause))
    assert await push.dynamic_push_callback(services, ORIGIN, sub) == 2
    assert calls == [first.url, push.DELIVERY_SPACING_SECONDS, later.url]


@pytest.mark.asyncio
async def test_dynamic_risk_control_pauses_discovery_and_keeps_queued_delivery(services, monkeypatch):
    sub = await subscribe(services)
    other_mid = "mock-second-uploader"
    await services.subscription_manager.add_subscription(ORIGIN, other_mid, "第二个 UP 主")
    other = await services.subscription_manager.get_subscription(ORIGIN, other_mid)
    await services.subscription_manager.discover_dynamics(ORIGIN, other_mid, [dynamic()], [ORIGIN])

    tick = [100.0]
    monkeypatch.setattr(push, "time", SimpleNamespace(time=time.time, monotonic=lambda: tick[0]))
    fetch = push.get_latest_dynamics
    fetch.side_effect = [RiskControlError(), RiskControlError(), []]

    assert (await push.check_dynamics(services, ORIGIN, sub)).discovery_failed
    result = await push.check_dynamics(services, ORIGIN, other)
    assert result == push.DynamicCheckResult(delivered=1, discovery_failed=True)
    assert fetch.await_count == 1

    tick[0] += push.DISCOVERY_RETRY_SECONDS
    assert (await push.check_dynamics(services, ORIGIN, sub)).discovery_failed
    assert services.dynamic_discovery_backoff[None][0] == 2 * push.DISCOVERY_RETRY_SECONDS
    tick[0] += 2 * push.DISCOVERY_RETRY_SECONDS
    assert not (await push.check_dynamics(services, ORIGIN, sub)).discovery_failed
    assert services.dynamic_discovery_backoff == {}


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
