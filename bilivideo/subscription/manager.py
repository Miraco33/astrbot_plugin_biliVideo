"""Subscription + push-target manager backed by `JsonStore`."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from ..core.types import LatestVideo
from .delivery import DeliveryJob
from .dynamic_delivery import DynamicDeliveryJob
from .store import JsonStore

if TYPE_CHECKING:
    from ..api.endpoints import LatestDynamic


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


def _withdraw_targets(queue: dict[str, Any], revoked: set[str]) -> None:
    for sessions in queue.values():
        for jobs in sessions.values():
            for job in jobs.values():
                if job.get("status") in {"completed", "cancelled"}:
                    continue
                withdrawn = job.setdefault("withdrawn", [])
                for target in job.get("targets", []):
                    if target in revoked and target not in withdrawn:
                        withdrawn.append(target)
                if all(target in withdrawn for target in job.get("targets", [])):
                    job["status"] = "cancelled"
                    job["error_kind"] = "TargetsRemoved"


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
            up = Subscription(mid=mid, name=name).to_dict()
            up_list.append({**up, "last_dynamic_at": int(time.time()) - 1})
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
            data.get("dynamic_jobs", {}).get(origin, {}).pop(mid, None)
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

    async def save_delivery_job(self, origin: str, mid: str, job: DeliveryJob) -> bool:
        saved = False

        def _mutate(data: dict[str, Any]) -> None:
            nonlocal saved
            entry = data.get("subscriptions", {}).get(origin) or {}
            up = next((item for item in entry.get("up_list", []) if item.get("mid") == mid), None)
            jobs = data.get("delivery_jobs", {}).get(origin, {}).get(mid, {})
            current = jobs.get(job.bvid)
            if up is None or current is None:
                return  # Unsubscribe/re-subscribe must not revive an old task.
            # Target removal can race with lengthy generation. Never overwrite
            # durable revocations with an old callback's in-memory snapshot.
            job.withdrawn = list(dict.fromkeys([*current.get("withdrawn", []), *job.withdrawn]))
            if current.get("status") == "cancelled" or all(
                target in job.withdrawn for target in job.targets
            ):
                job.status = "cancelled"
                job.error_kind = "TargetsRemoved"
            else:
                job.update_status()
            jobs[job.bvid] = job.to_dict()
            saved = True
            if job.status == "completed" and job.bvid == up.get("last_seen_bvid"):
                up["last_bvid"] = job.bvid

        await self._store.mutate(_mutate)
        return saved

    # ------------------------------------------------------------------
    # public dynamics use their own cursor and target acknowledgements
    # ------------------------------------------------------------------
    async def discover_dynamics(
        self, origin: str, mid: str, entries: list[LatestDynamic], targets: list[str],
    ) -> None:
        chronological = sorted(entries, key=lambda entry: entry.pubdate)
        now = int(time.time())

        def _mutate(data: dict[str, Any]) -> None:
            entry = data.get("subscriptions", {}).get(origin) or {}
            up = next((item for item in entry.get("up_list", []) if item.get("mid") == mid), None)
            if up is None:
                return
            previous_at = up.get("last_dynamic_at")
            newest_at = chronological[-1].pubdate if chronological else now - 1
            if previous_at is None:
                # Existing subscriptions establish a baseline on the first successful poll.
                up["last_dynamic_at"] = newest_at
                up["last_dynamic_ids"] = [
                    item.dynamic_id for item in chronological if item.pubdate == newest_at
                ]
                return
            existing_jobs = data.get("dynamic_jobs", {}).get(origin, {}).get(mid, {})
            for item in chronological:
                recorded = existing_jobs.get(item.dynamic_id)
                if (
                    recorded is not None and recorded.get("status") not in {"completed", "cancelled"}
                    and not recorded.get("image_urls") and item.image_urls
                ):
                    recorded["image_urls"] = list(item.image_urls)
            seen_at_cursor = set(up.get("last_dynamic_ids", []))
            new_entries = [
                item for item in chronological
                if item.pubdate > previous_at
                or (item.pubdate == previous_at and item.dynamic_id not in seen_at_cursor)
            ]
            if not new_entries:
                return
            jobs = data.setdefault("dynamic_jobs", {}).setdefault(origin, {}).setdefault(mid, {})
            for item in new_entries:
                if item.kind == "DYNAMIC_TYPE_AV" and item.bvid:
                    continue  # Video submissions are covered by the video subscription.
                jobs.setdefault(
                    item.dynamic_id,
                    DynamicDeliveryJob(
                        dynamic_id=item.dynamic_id, text=item.text, url=item.url,
                        pubdate=item.pubdate, targets=list(dict.fromkeys(targets)),
                        image_urls=list(item.image_urls),
                    ).to_dict(),
                )

            up["last_dynamic_at"] = newest_at
            if newest_at > previous_at:
                seen_at_cursor.clear()
            seen_at_cursor.update(item.dynamic_id for item in new_entries if item.pubdate == newest_at)
            up["last_dynamic_ids"] = sorted(seen_at_cursor)

        await self._store.mutate(_mutate)

    async def get_dynamic_jobs(self, origin: str, mid: str) -> list[DynamicDeliveryJob]:
        data = await self._store.read()
        jobs = data.get("dynamic_jobs", {}).get(origin, {}).get(mid, {})
        return [DynamicDeliveryJob.from_mapping(raw) for raw in jobs.values()]

    async def save_dynamic_job(self, origin: str, mid: str, job: DynamicDeliveryJob) -> bool:
        saved = False

        def _mutate(data: dict[str, Any]) -> None:
            nonlocal saved
            jobs = data.get("dynamic_jobs", {}).get(origin, {}).get(mid, {})
            current = jobs.get(job.dynamic_id)
            if current is None:
                return
            job.withdrawn = list(dict.fromkeys([*current.get("withdrawn", []), *job.withdrawn]))
            if current.get("status") == "cancelled" or all(
                target in job.withdrawn for target in job.targets
            ):
                job.status = "cancelled"
                job.error_kind = "TargetsRemoved"
            jobs[job.dynamic_id] = job.to_dict()
            saved = True
            if job.status == "completed":
                completed = [key for key, value in jobs.items() if value.get("status") == "completed"]
                for key in completed[:-20]:
                    jobs.pop(key)

        await self._store.mutate(_mutate)
        return saved

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
            revoked = {target.get("origin") for target in targets if target not in new_targets}
            data["push_targets"] = new_targets
            if revoked:
                for queue in ("delivery_jobs", "dynamic_jobs"):
                    _withdraw_targets(data.get(queue, {}), revoked)

        await self._store.mutate(_mutate)
        return removed

    async def get_push_targets(self) -> list[PushTarget]:
        data = await self._store.read()
        return [PushTarget(origin=t.get("origin", ""), label=t.get("label", ""))
                for t in (data.get("push_targets") or [])]

    async def get_push_origins(self) -> list[str]:
        return [t.origin for t in await self.get_push_targets()]
