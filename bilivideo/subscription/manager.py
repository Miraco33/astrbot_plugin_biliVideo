"""Subscription + push-target manager backed by `JsonStore`."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from ..core.types import LatestVideo
from .delivery import DeliveryJob
from .store import JsonStore


@dataclass(slots=True)
class Subscription:
    mid: str
    name: str
    last_bvid: str = ""
    last_seen_bvid: str = ""

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> Subscription:
        return cls(
            mid=str(raw.get("mid", "")),
            name=str(raw.get("name", "")),
            last_bvid=str(raw.get("last_bvid", "")),
            last_seen_bvid=str(raw.get("last_seen_bvid", raw.get("last_bvid", ""))),
        )

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(slots=True, frozen=True)
class PushTarget:
    origin: str
    label: str

    def to_dict(self) -> dict[str, str]:
        return {"origin": self.origin, "label": self.label}


_DEFAULT = {"subscriptions": {}, "push_targets": []}


class SubscriptionManager:
    """High-level subscription operations.

    All mutations go through `JsonStore.mutate`, ensuring atomic disk writes.
    """

    def __init__(self, data_dir: str) -> None:
        self._store = JsonStore(f"{data_dir.rstrip('/')}/subscriptions.json", default=_DEFAULT)

    def close(self) -> None:
        """Refuse further writes; called on plugin shutdown/hot-reload."""

        self._store.close()

    # ------------------------------------------------------------------
    # subscriptions
    # ------------------------------------------------------------------
    async def add_subscription(self, origin: str, mid: str, name: str) -> bool:
        added = False

        def _mutate(data: dict[str, Any]) -> None:
            nonlocal added
            subs = data.setdefault("subscriptions", {}).setdefault(origin, {"up_list": []})
            up_list = subs.setdefault("up_list", [])
            if any(up.get("mid") == mid for up in up_list):
                return
            up_list.append(Subscription(mid=mid, name=name).to_dict())
            added = True

        await self._store.mutate(_mutate)
        return added

    async def remove_subscription(self, origin: str, mid: str) -> bool:
        removed = False

        def _mutate(data: dict[str, Any]) -> None:
            nonlocal removed
            subs = data.get("subscriptions", {})
            entry = subs.get(origin)
            if not entry:
                return
            before = len(entry.get("up_list", []))
            entry["up_list"] = [up for up in entry.get("up_list", []) if up.get("mid") != mid]
            removed = len(entry["up_list"]) < before
            data.get("delivery_jobs", {}).get(origin, {}).pop(mid, None)
            if not entry["up_list"]:
                subs.pop(origin, None)

        await self._store.mutate(_mutate)
        return removed

    async def get_subscriptions(self, origin: str) -> list[Subscription]:
        data = await self._store.read()
        entry = data.get("subscriptions", {}).get(origin) or {}
        return [Subscription.from_mapping(up) for up in entry.get("up_list", [])]

    async def get_subscription(self, origin: str, mid: str) -> Subscription | None:
        """Fresh single-subscription read; push paths re-check state under lock."""
        for sub in await self.get_subscriptions(origin):
            if sub.mid == mid:
                return sub
        return None

    async def get_subscription_count(self, origin: str) -> int:
        return len(await self.get_subscriptions(origin))

    async def all_subscriptions(self) -> dict[str, list[Subscription]]:
        data = await self._store.read()
        out: dict[str, list[Subscription]] = {}
        for origin, entry in (data.get("subscriptions") or {}).items():
            out[origin] = [Subscription.from_mapping(up) for up in entry.get("up_list", [])]
        return out

    async def update_last_video(self, origin: str, mid: str, bvid: str) -> None:
        def _mutate(data: dict[str, Any]) -> None:
            entry = data.get("subscriptions", {}).get(origin)
            if not entry:
                return
            for up in entry.get("up_list", []):
                if up.get("mid") == mid:
                    up["last_bvid"] = bvid
                    up["last_seen_bvid"] = bvid
                    return

        await self._store.mutate(_mutate)


    # ------------------------------------------------------------------
    # durable delivery queue; old subscriptions.json files migrate lazily
    # ------------------------------------------------------------------
    async def discover_video(
        self, origin: str, mid: str, latest: LatestVideo, targets: list[str], *, auto_summary: bool
    ) -> None:
        def _mutate(data: dict[str, Any]) -> None:
            entry = data.get("subscriptions", {}).get(origin) or {}
            up = next((item for item in entry.get("up_list", []) if item.get("mid") == mid), None)
            if up is None:
                return
            seen = up.get("last_seen_bvid", up.get("last_bvid", ""))
            if latest.bvid == seen:
                return
            up["last_seen_bvid"] = latest.bvid
            if not seen:
                up["last_bvid"] = latest.bvid
                return  # Preserve the old first-sighting baseline behavior.
            jobs = data.setdefault("delivery_jobs", {}).setdefault(origin, {}).setdefault(mid, {})
            jobs.setdefault(
                latest.bvid,
                DeliveryJob(
                    bvid=latest.bvid, video=asdict(latest),
                    targets=list(dict.fromkeys(targets)), auto_summary=auto_summary,
                ).to_dict(),
            )
            # Retain pending/exhausted jobs for compensation; trim only old
            # completed records so failure cannot silently discard a video.
            completed = [key for key, value in jobs.items() if value.get("status") == "completed"]
            for key in completed[:-10]:
                jobs.pop(key)

        await self._store.mutate(_mutate)

    async def get_delivery_jobs(self, origin: str, mid: str) -> list[DeliveryJob]:
        data = await self._store.read()
        jobs = data.get("delivery_jobs", {}).get(origin, {}).get(mid, {})
        return [DeliveryJob.from_mapping(raw) for raw in jobs.values()]

    async def save_delivery_job(self, origin: str, mid: str, job: DeliveryJob) -> None:
        def _mutate(data: dict[str, Any]) -> None:
            entry = data.get("subscriptions", {}).get(origin) or {}
            up = next((item for item in entry.get("up_list", []) if item.get("mid") == mid), None)
            if up is None:
                return
            jobs = data.setdefault("delivery_jobs", {}).setdefault(origin, {}).setdefault(mid, {})
            jobs[job.bvid] = job.to_dict()
            # Completion is independent of discovery and failure notices.
            # A late older retry must not overwrite the newest completion.
            if job.status == "completed" and job.bvid == up.get("last_seen_bvid"):
                up["last_bvid"] = job.bvid

        await self._store.mutate(_mutate)

    # ------------------------------------------------------------------
    # push targets
    # ------------------------------------------------------------------
    async def add_push_target(self, origin: str, label: str = "") -> bool:
        added = False

        def _mutate(data: dict[str, Any]) -> None:
            nonlocal added
            targets = data.setdefault("push_targets", [])
            if any(t.get("origin") == origin for t in targets):
                return
            targets.append(PushTarget(origin=origin, label=label).to_dict())
            added = True

        await self._store.mutate(_mutate)
        return added

    async def remove_push_target(self, identifier: str) -> bool:
        removed = False

        def _mutate(data: dict[str, Any]) -> None:
            nonlocal removed
            targets = data.get("push_targets", [])
            new_targets = [
                t for t in targets
                if t.get("label") != identifier and t.get("origin") != identifier
            ]
            removed = len(new_targets) < len(targets)
            data["push_targets"] = new_targets

        await self._store.mutate(_mutate)
        return removed

    async def get_push_targets(self) -> list[PushTarget]:
        data = await self._store.read()
        return [PushTarget(origin=t.get("origin", ""), label=t.get("label", ""))
                for t in (data.get("push_targets") or [])]

    async def get_push_origins(self) -> list[str]:
        return [t.origin for t in await self.get_push_targets()]
