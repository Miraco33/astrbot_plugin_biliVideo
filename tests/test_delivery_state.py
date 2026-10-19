"""Offline compensation tests with synthetic videos and local temporary state."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bilivideo.access.keyed_lock import KeyedLocks
from bilivideo.core.exceptions import DownloadError, NetworkError
from bilivideo.core.types import LatestVideo, VideoInfo
from bilivideo.handlers import scheduled_push as push
from bilivideo.handlers import subscription as commands
from bilivideo.subscription.delivery import MAX_ATTEMPTS
from bilivideo.subscription.manager import SubscriptionManager

ORIGIN = "mock-origin"
MID = "mock-uploader"
VIDEO = LatestVideo(bvid="BV1mockvideo", title="Synthetic video")
INFO = VideoInfo(bvid=VIDEO.bvid, title=VIDEO.title)


@pytest.fixture
def services(tmp_path, monkeypatch):
    manager = SubscriptionManager(str(tmp_path))
    monkeypatch.setattr(push, "MessageChain", lambda **kwargs: kwargs)
    monkeypatch.setattr(push, "Plain", lambda value: value)
    monkeypatch.setattr(push, "get_latest_videos", AsyncMock(return_value=[VIDEO]))
    monkeypatch.setattr(push, "render_note_components", AsyncMock(return_value="summary"))
    return SimpleNamespace(
        subscription_manager=manager,
        astrbot_context=SimpleNamespace(send_message=AsyncMock(return_value=True)),
        push_locks=KeyedLocks(),
        http_client=object(),
        logger=MagicMock(),
        config=SimpleNamespace(auto_push_summary=True, enable_forward_message=False),
        orchestrator=SimpleNamespace(
            generate=AsyncMock(return_value=SimpleNamespace(markdown="summary", video_info=INFO))
        ),
    )


async def seed(services):
    manager = services.subscription_manager
    await manager.add_subscription(ORIGIN, MID, "Synthetic uploader")
    await manager.update_last_video(ORIGIN, MID, "BV1mockold")
    return await manager.get_subscription(ORIGIN, MID)


async def job_of(services):
    return (await services.subscription_manager.get_delivery_jobs(ORIGIN, MID))[0]


@pytest.mark.asyncio
async def test_failure_notice_does_not_complete_then_retry_succeeds(services, monkeypatch):
    sub = await seed(services)
    services.orchestrator.generate.side_effect = [
        DownloadError("synthetic failure"),
        SimpleNamespace(markdown="summary", video_info=INFO),
    ]
    assert await push.push_callback(services, ORIGIN, sub) == 0
    failed = await job_of(services)
    assert failed.summary_status == "failed"
    assert failed.notified == [ORIGIN]
    assert (await services.subscription_manager.get_subscription(ORIGIN, MID)).last_bvid == "BV1mockold"
    assert await push.push_callback(services, ORIGIN, sub) == 0
    assert services.orchestrator.generate.await_count == 1  # automatic backoff
    monkeypatch.setattr(push.time, "time", lambda: failed.next_retry_at + 1)
    assert await push.push_callback(services, ORIGIN, sub) == 1
    assert (await job_of(services)).status == "completed"
    assert (await services.subscription_manager.get_subscription(ORIGIN, MID)).last_bvid == VIDEO.bvid


@pytest.mark.asyncio
@pytest.mark.parametrize("send_result", [False, None])
async def test_no_throw_is_not_positive_receipt(services, send_result):
    sub = await seed(services)
    services.astrbot_context.send_message.return_value = send_result
    assert await push.push_callback(services, ORIGIN, sub) == 0
    job = await job_of(services)
    assert job.delivered == []
    assert job.notified == []
    assert job.summary_status == "ready"
    assert job.error_kind == "SendNotConfirmed"
    assert (await services.subscription_manager.get_subscription(ORIGIN, MID)).last_bvid == "BV1mockold"


@pytest.mark.asyncio
async def test_partial_delivery_survives_reload_without_regeneration(services, tmp_path):
    sub = await seed(services)
    manager = services.subscription_manager
    await manager.add_push_target("mock-target-a")
    await manager.add_push_target("mock-target-b")
    services.astrbot_context.send_message.side_effect = [True, False, True]
    assert await push.push_callback(services, ORIGIN, sub) == 0
    assert (await job_of(services)).delivered == ["mock-target-a"]
    manager.close()
    services.subscription_manager = SubscriptionManager(str(tmp_path))
    services.astrbot_context.send_message.reset_mock(side_effect=True)
    services.astrbot_context.send_message.return_value = True
    assert await push.push_callback(services, ORIGIN, sub, force=True) == 1
    assert services.orchestrator.generate.await_count == 1
    calls = services.astrbot_context.send_message.await_args_list
    assert len(calls) == 1
    assert calls[0].args[0] == "mock-target-b"
    assert (await job_of(services)).notified == ["mock-target-b"]


@pytest.mark.asyncio
async def test_existing_job_retries_when_discovery_fails(services, monkeypatch):
    sub = await seed(services)
    services.orchestrator.generate.side_effect = DownloadError("synthetic")
    await push.push_callback(services, ORIGIN, sub)
    services.orchestrator.generate.side_effect = None
    monkeypatch.setattr(push, "get_latest_videos", AsyncMock(side_effect=NetworkError()))
    assert await push.push_callback(services, ORIGIN, sub, force=True) == 1


@pytest.mark.asyncio
async def test_budget_survives_reload_and_notice_is_sent_once(services, tmp_path):
    sub = await seed(services)
    services.orchestrator.generate.side_effect = DownloadError("synthetic")
    await push.push_callback(services, ORIGIN, sub)
    for _ in range(MAX_ATTEMPTS - 1):
        job = await job_of(services)
        job.next_retry_at = 0
        await services.subscription_manager.save_delivery_job(ORIGIN, MID, job)
        await push.push_callback(services, ORIGIN, sub)
    assert (await job_of(services)).status == "exhausted"
    assert services.astrbot_context.send_message.await_count == 1
    services.subscription_manager.close()
    services.subscription_manager = SubscriptionManager(str(tmp_path))
    await push.push_callback(services, ORIGIN, sub)
    assert services.orchestrator.generate.await_count == MAX_ATTEMPTS
    services.orchestrator.generate.side_effect = None
    assert await push.push_callback(services, ORIGIN, sub, force=True) == 1
    assert (await job_of(services)).attempts == 1


@pytest.mark.asyncio
async def test_concurrent_manual_and_scheduled_share_state(services):
    sub = await seed(services)
    results = await asyncio.gather(
        push.push_callback(services, ORIGIN, sub),
        push.push_callback(services, ORIGIN, sub, force=True),
    )
    assert sum(results) == 1
    assert services.orchestrator.generate.await_count == 1
    assert services.astrbot_context.send_message.await_count == 1


@pytest.mark.asyncio
async def test_legacy_store_migrates_without_losing_fields(services, tmp_path):
    legacy = {
        "subscriptions": {
            ORIGIN: {"up_list": [{"mid": MID, "name": "Synthetic", "last_bvid": "BV1mockold", "extra": 7}]}
        },
        "push_targets": [{"origin": "mock-target", "label": "Synthetic"}],
        "unrelated": {"keep": True},
    }
    services.subscription_manager.close()
    target = tmp_path / "subscriptions.json"
    target.write_text(json.dumps(legacy), encoding="utf-8")
    services.subscription_manager = SubscriptionManager(str(tmp_path))
    sub = await services.subscription_manager.get_subscription(ORIGIN, MID)
    assert sub.last_seen_bvid == "BV1mockold"
    await push.push_callback(services, ORIGIN, sub)
    persisted = json.loads(target.read_text(encoding="utf-8"))
    assert persisted["unrelated"] == legacy["unrelated"]
    assert persisted["push_targets"] == legacy["push_targets"]
    assert persisted["subscriptions"][ORIGIN]["up_list"][0]["extra"] == 7
    assert persisted["delivery_jobs"][ORIGIN][MID][VIDEO.bvid]["status"] == "completed"


@pytest.mark.asyncio
async def test_manual_command_uses_shared_retry_path(services, monkeypatch):
    sub = await seed(services)
    services.orchestrator.generate.side_effect = DownloadError("synthetic")
    await push.push_callback(services, ORIGIN, sub)
    services.orchestrator.generate.side_effect = None
    monkeypatch.setattr(commands.asyncio, "sleep", AsyncMock())
    event = SimpleNamespace(unified_msg_origin=ORIGIN, plain_result=lambda value: value)
    replies = [
        reply async for reply in commands.handle_check_updates.__wrapped__(services, event)
    ]
    assert "本次确认完成投递 1 个" in replies[-1]
    assert (await job_of(services)).status == "completed"


@pytest.mark.asyncio
async def test_first_sighting_still_only_sets_baseline(services):
    await services.subscription_manager.add_subscription(ORIGIN, MID, "Synthetic")
    sub = await services.subscription_manager.get_subscription(ORIGIN, MID)
    assert await push.push_callback(services, ORIGIN, sub) == 0
    assert await services.subscription_manager.get_delivery_jobs(ORIGIN, MID) == []
    services.orchestrator.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_keeps_budget_and_recovers_after_reload(services, tmp_path):
    sub = await seed(services)
    started = asyncio.Event()

    async def wait_forever(url):
        started.set()
        await asyncio.Future()

    services.orchestrator.generate.side_effect = wait_forever
    task = asyncio.create_task(push.push_callback(services, ORIGIN, sub))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await job_of(services)).attempts == 1
    services.subscription_manager.close()
    services.subscription_manager = SubscriptionManager(str(tmp_path))
    services.orchestrator.generate.side_effect = None
    assert await push.push_callback(services, ORIGIN, sub, force=True) == 1


@pytest.mark.asyncio
async def test_newer_discovery_does_not_discard_failed_job(services, monkeypatch):
    sub = await seed(services)
    services.orchestrator.generate.side_effect = DownloadError("synthetic")
    await push.push_callback(services, ORIGIN, sub)
    newer = LatestVideo(bvid="BV1mocknewer", title="New synthetic")
    monkeypatch.setattr(push, "get_latest_videos", AsyncMock(return_value=[newer]))
    services.orchestrator.generate.side_effect = None
    assert await push.push_callback(services, ORIGIN, sub, force=True) == 1
    jobs = await services.subscription_manager.get_delivery_jobs(ORIGIN, MID)
    assert [job.bvid for job in jobs] == [VIDEO.bvid, newer.bvid]
    assert jobs[0].status == "completed"
    assert jobs[1].status == "pending"
    # The old retry cannot move the completion cursor past the newer job.
    assert (await services.subscription_manager.get_subscription(ORIGIN, MID)).last_bvid == "BV1mockold"
