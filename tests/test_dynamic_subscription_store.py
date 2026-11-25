"""Offline coverage of dynamic cursors, queues and target revocation."""

from __future__ import annotations

import json

import pytest

from bilivideo.api.endpoints import LatestDynamic
from bilivideo.subscription.manager import SubscriptionManager


def item(post_id: str, pubdate: int, *, kind: str = "DYNAMIC_TYPE_WORD", bvid: str = "") -> LatestDynamic:
    return LatestDynamic(
        dynamic_id=post_id, pubdate=pubdate, text="合成动态",
        url=f"https://t.bilibili.com/{post_id}", kind=kind, bvid=bvid,
    )


@pytest.mark.asyncio
async def test_existing_subscription_seeds_dynamics_without_resending_history(tmp_path, monkeypatch):
    monkeypatch.setattr("bilivideo.subscription.manager.time.time", lambda: 50)
    (tmp_path / "subscriptions.json").write_text(
        json.dumps({"subscriptions": {"o": {"up_list": [{"mid": "7", "name": "Test", "last_bvid": "old"}]}}}),
        encoding="utf-8",
    )
    mgr = SubscriptionManager(str(tmp_path))
    await mgr.discover_dynamics("o", "7", [item("2", 20), item("1", 10)], ["o"])
    assert not await mgr.get_dynamic_jobs("o", "7")
    sub = await mgr.get_subscription("o", "7")
    assert sub is not None and sub.last_bvid == "old"

    await mgr.discover_dynamics("o", "7", [item("2", 20), item("3", 30), item("4", 30)], ["o"])
    mgr = SubscriptionManager(str(tmp_path))
    await mgr.discover_dynamics("o", "7", [item("1", 10), item("3", 30), item("5", 30)], ["o"])
    jobs = await mgr.get_dynamic_jobs("o", "7")
    assert [job.dynamic_id for job in jobs] == ["3", "4", "5"]


@pytest.mark.asyncio
async def test_new_subscription_keeps_first_fresh_dynamic_and_leaves_own_video_to_video_queue(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr("bilivideo.subscription.manager.time.time", lambda: 50)
    mgr = SubscriptionManager(str(tmp_path))
    assert await mgr.add_subscription("o", "7", "Test")
    await mgr.discover_dynamics(
        "o", "7",
        [item("1", 40), item("2", 51, kind="DYNAMIC_TYPE_AV", bvid="BV1test00001"),
         item("3", 52, kind="DYNAMIC_TYPE_FORWARD")],
        ["o", "o"],
    )
    jobs = await mgr.get_dynamic_jobs("o", "7")
    assert [(job.dynamic_id, job.targets) for job in jobs] == [("3", ["o"])]


@pytest.mark.asyncio
async def test_dynamic_receipts_survive_target_removal_reload_and_unsubscribe(tmp_path, monkeypatch):
    monkeypatch.setattr("bilivideo.subscription.manager.time.time", lambda: 50)
    mgr = SubscriptionManager(str(tmp_path))
    await mgr.add_subscription("o", "7", "Test")
    await mgr.add_push_target("a")
    await mgr.add_push_target("b")
    await mgr.discover_dynamics("o", "7", [item("3", 51)], ["a", "b"])
    job, = await mgr.get_dynamic_jobs("o", "7")
    job.begin_attempt(51)
    job.delivered.append("a")
    assert await mgr.remove_push_target("b")
    assert await mgr.save_dynamic_job("o", "7", job)

    mgr = SubscriptionManager(str(tmp_path))
    saved, = await mgr.get_dynamic_jobs("o", "7")
    assert saved.withdrawn == ["b"]
    assert saved.delivered == ["a"]
    assert saved.all_delivered
    assert not saved.due(52)
    assert saved.due(52, force=True)

    assert await mgr.remove_subscription("o", "7")
    assert not await mgr.save_dynamic_job("o", "7", saved)
    assert not await mgr.get_dynamic_jobs("o", "7")
