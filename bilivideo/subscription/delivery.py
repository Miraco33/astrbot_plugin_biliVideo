"""Durable delivery records; runtime data stays in the existing private store."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

MAX_ATTEMPTS = 4
RETRY_DELAYS = (300, 1800, 7200, 21600)


@dataclass(slots=True)
class DeliveryJob:
    bvid: str
    video: dict[str, Any] = field(default_factory=dict)
    targets: list[str] = field(default_factory=list)
    delivered: list[str] = field(default_factory=list)
    notified: list[str] = field(default_factory=list)
    summary_status: str = "pending"
    markdown: str = ""
    video_info: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    next_retry_at: float = 0
    status: str = "pending"
    error_kind: str = ""
    auto_summary: bool = True

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> DeliveryJob:
        fields = cls.__dataclass_fields__
        return cls(**{key: value for key, value in raw.items() if key in fields})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def due(self, now: float, *, force: bool = False) -> bool:
        if self.status == "completed":
            return False
        return force or (self.attempts < MAX_ATTEMPTS and now >= self.next_retry_at)

    def begin_attempt(self, now: float, *, force: bool = False) -> None:
        if force and self.attempts >= MAX_ATTEMPTS:
            self.attempts = 0
        self.attempts += 1
        self.status = "retry"
        # Save a delay before external work. Cancellation/reload cannot produce
        # a tight retry loop, and attempts already consumed remain consumed.
        self.next_retry_at = now + RETRY_DELAYS[min(self.attempts - 1, len(RETRY_DELAYS) - 1)]

    def fail(self, kind: str) -> None:
        self.error_kind = kind
        self.status = "exhausted" if self.attempts >= MAX_ATTEMPTS else "retry"

    @property
    def all_delivered(self) -> bool:
        return bool(self.targets) and all(target in self.delivered for target in self.targets)
