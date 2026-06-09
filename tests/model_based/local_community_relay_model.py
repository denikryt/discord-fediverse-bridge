"""Independent state model for local-community relay delivery lifecycle."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

DeliveryStatus = Literal["pending", "failed", "delivered"]


@dataclass(slots=True)
class ModelDelivery:
    """Track product-relevant state for one target actor."""

    status: DeliveryStatus = "pending"
    attempt_count: int = 0
    relay_activity_id: str | None = None
    last_error: str | None = None


@dataclass(slots=True)
class RelayModel:
    """Model one relay source and its per-target durable delivery state."""

    source_exists: bool = False
    deliveries: dict[str, ModelDelivery] = field(default_factory=dict)
    gateway_calls: list[tuple[str, ...]] = field(default_factory=list)

    def relay_create(
        self,
        *,
        source_json_present: bool,
        allowed_targets: set[str],
        outcomes: dict[str, tuple[bool, str | None, str | None]],
    ) -> None:
        """Apply one create/retry action using independent target and outcome inputs."""
        if not source_json_present:
            return
        self.source_exists = True
        for actor_id in allowed_targets:
            self.deliveries.setdefault(actor_id, ModelDelivery())

        attempted = tuple(
            sorted(
                actor_id
                for actor_id, delivery in self.deliveries.items()
                if delivery.status in {"pending", "failed"}
            )
        )
        if not attempted:
            return
        self.gateway_calls.append(attempted)
        for actor_id in attempted:
            ok, activity_id, error = outcomes[actor_id]
            delivery = self.deliveries[actor_id]
            delivery.attempt_count += 1
            delivery.relay_activity_id = activity_id or delivery.relay_activity_id
            delivery.last_error = error
            delivery.status = "delivered" if ok else "failed"
