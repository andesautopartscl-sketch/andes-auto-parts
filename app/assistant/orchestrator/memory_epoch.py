"""FASE 7B.2 — permission_epoch resolution for memory reads.

Does NOT create or mutate ERP permissions.
There is currently no robust integer epoch source in the ERP; resolution is
neutral (available=False) until a real source is wired.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class PermissionEpochResolution:
    """Result of resolving the actor's current permission epoch."""

    available: bool
    """True only when a robust ERP/source epoch was obtained."""

    epoch: int | None
    """Current epoch when available; None when unavailable/neutral."""


class PermissionEpochProvider(Protocol):
    def resolve(self, actor_user: str) -> PermissionEpochResolution:
        ...


class NeutralPermissionEpochProvider:
    """Default: no ERP epoch source — do not invent one; skip mismatch filtering."""

    def resolve(self, actor_user: str) -> PermissionEpochResolution:
        _ = (actor_user or "").strip()
        return PermissionEpochResolution(available=False, epoch=None)


_DEFAULT_PROVIDER: PermissionEpochProvider = NeutralPermissionEpochProvider()


def get_default_permission_epoch_provider() -> PermissionEpochProvider:
    return _DEFAULT_PROVIDER


def set_permission_epoch_provider_for_tests(provider: PermissionEpochProvider | None) -> None:
    """Tests only — inject a fake provider or restore Neutral."""
    global _DEFAULT_PROVIDER
    _DEFAULT_PROVIDER = provider or NeutralPermissionEpochProvider()


def resolve_actor_permission_epoch(
    actor_user: str,
    *,
    provider: PermissionEpochProvider | None = None,
) -> PermissionEpochResolution:
    prov = provider or get_default_permission_epoch_provider()
    try:
        return prov.resolve(actor_user)
    except Exception:
        return PermissionEpochResolution(available=False, epoch=None)


def memory_passes_permission_epoch(
    slot: dict[str, Any],
    resolution: PermissionEpochResolution,
) -> bool:
    """Enforce read-time epoch rules without mutating permissions.

    - If resolution.available is False → neutral: keep slot.
    - sensitivity=benign → may survive epoch mismatch (approved design).
    - sensitivity=contextual → require matching permission_epoch when available.
    """
    if not resolution.available or resolution.epoch is None:
        return True

    sens = str(slot.get("sensitivity") or "benign").strip().lower()
    if sens == "benign":
        return True

    raw = slot.get("permission_epoch")
    if raw is None:
        # Contextual without epoch cannot be proven compatible → exclude
        return False
    try:
        return int(raw) == int(resolution.epoch)
    except (TypeError, ValueError):
        return False
