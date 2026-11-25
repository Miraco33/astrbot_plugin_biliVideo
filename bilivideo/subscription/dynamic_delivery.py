"""Persistent delivery state for Bilibili dynamics."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .delivery import MAX_ATTEMPTS, RETRY_DELAYS


@dataclass(slots=True)
class DynamicDeliveryJob:
    dynamic_id: str
    text: str
    url: str
    pubdate: int
    targets: list[str]
    delivered: list[str] = field(default_factory=list)
    withdrawn: list[str] = field(default_factory=list)
    attempts: int = 0
    next_retry_at: float = 0
    status: str = "pending"
    error_kind: str = ""

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> DynamicDeliveryJob:
        return cls(**{key: value for key, value in raw.items() if key in cls.__dataclass_fields__})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def due(self, now: float, *, force: bool = False) -> bool:
        if self.status in {"completed", "cancelled"}:
            return False
        return force or (self.attempts < MAX_ATTEMPTS and now >= self.next_retry_at)

    def begin_attempt(self, now: float, *, force: bool = False) -> None:
        if force and self.attempts >= MAX_ATTEMPTS:
            self.attempts = 0
        self.attempts += 1
        self.status = "exhausted" if self.attempts >= MAX_ATTEMPTS else "retry"
        self.next_retry_at = now + RETRY_DELAYS[min(self.attempts - 1, len(RETRY_DELAYS) - 1)]

    def fail(self, kind: str) -> None:
        self.error_kind = kind
        self.status = "exhausted" if self.attempts >= MAX_ATTEMPTS else "retry"

    @property
    def all_delivered(self) -> bool:
        active = [target for target in self.targets if target not in self.withdrawn]
        return bool(active) and all(target in self.delivered for target in active)
