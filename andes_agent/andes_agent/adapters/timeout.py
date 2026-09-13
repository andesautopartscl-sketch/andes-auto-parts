"""Timeout defaults for the future ERP HTTP adapter."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TimeoutConfig:
    connect_seconds: float = 2.0
    read_seconds: float = 5.0
    total_seconds: float = 8.0
