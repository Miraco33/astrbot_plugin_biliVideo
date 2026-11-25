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
    withdrawn: list[str] = field(default_factory=list)
    summary_status: str = "pending"
    markdown: str = ""
    video_info: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    next_retry_at: float = 0
    status: str = "pending"
    error_kind: str = ""
    auto_summary: bool = True
    summary_delivered: list[str] = field(default_factory=list)
    summary_notified: list[str] = field(default_factory=list)
    summary_attempts: int = 0
    summary_next_retry_at: float = 0
    summary_error_kind: str = ""

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> DeliveryJob:
        fields = cls.__dataclass_fields__
        job = cls(**{key: value for key, value in raw.items() if key in fields})
        if "summary_delivered" not in raw and job.auto_summary:
            # Earlier versions sent one combined message, after generating the summary.
            job.summary_delivered = job.delivered.copy()
            job.summary_notified = job.notified.copy()
            if job.summary_status != "ready":
                job.summary_attempts = job.attempts
                job.summary_next_retry_at = job.next_retry_at
                job.summary_error_kind = job.error_kind
            job.notified = []
            job.attempts = 0
            job.next_retry_at = 0
            job.error_kind = ""
        return job

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def active_targets(self) -> list[str]:
        return [target for target in self.targets if target not in self.withdrawn]

    @property
    def basic_targets(self) -> list[str]:
        return [target for target in self.active_targets if target not in self.delivered]

    @property
    def summary_targets(self) -> list[str]:
        if not self.auto_summary:
            return []
        return [
            target for target in self.active_targets
            if target in self.delivered and target not in self.summary_delivered
        ]

    def basic_due(self, now: float, *, force: bool = False) -> bool:
        return bool(self.basic_targets) and (
            force or (self.attempts < MAX_ATTEMPTS and now >= self.next_retry_at)
        )

    def summary_due(self, now: float, *, force: bool = False) -> bool:
        return bool(self.summary_targets) and (
            force or (self.summary_attempts < MAX_ATTEMPTS and now >= self.summary_next_retry_at)
        )

    def begin_attempt(self, now: float, *, force: bool = False) -> None:
        self.attempts, self.next_retry_at = _next_attempt(self.attempts, now, force=force)

    def begin_summary_attempt(self, now: float, *, force: bool = False) -> None:
        self.summary_attempts, self.summary_next_retry_at = _next_attempt(
            self.summary_attempts, now, force=force
        )

    def record_basic_delivery(self, target: str) -> None:
        # A target that just received its basic message can still get the cached summary.
        if self.summary_status == "ready" and (
            not self.summary_targets or self.summary_attempts >= MAX_ATTEMPTS
        ):
            self.summary_attempts = 0
            self.summary_next_retry_at = 0
        self.delivered.append(target)

    def update_status(self) -> None:
        if not self.active_targets:
            self.status = "cancelled"
            self.error_kind = "TargetsRemoved"
            return
        basic_pending = bool(self.basic_targets)
        summary_pending = bool(self.summary_targets)
        if not basic_pending:
            self.error_kind = ""
            self.next_retry_at = 0
        if not summary_pending and self.summary_status == "ready":
            self.summary_error_kind = ""
            self.summary_next_retry_at = 0
        if not basic_pending and not summary_pending:
            self.status = "completed"
        elif (basic_pending and self.attempts < MAX_ATTEMPTS) or (
            summary_pending and self.summary_attempts < MAX_ATTEMPTS
        ):
            self.status = "retry" if self.attempts or self.summary_attempts else "pending"
        else:
            self.status = "exhausted"


def _next_attempt(attempts: int, now: float, *, force: bool) -> tuple[int, float]:
    if force and attempts >= MAX_ATTEMPTS:
        attempts = 0
    attempts += 1
    # Save the delay before external work so cancellation also consumes an attempt.
    return attempts, now + RETRY_DELAYS[min(attempts - 1, len(RETRY_DELAYS) - 1)]
