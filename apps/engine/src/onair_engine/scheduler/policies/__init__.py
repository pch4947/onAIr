from __future__ import annotations

from .base import SchedulingPolicy
from .naive_fifo import NaiveFifoPolicy

__all__ = ["SchedulingPolicy", "get_policy"]

_POLICIES: dict[str, type] = {
    NaiveFifoPolicy.name: NaiveFifoPolicy,
    # TODO(M4): 정책 A/B/C — 기획서 8장 확정 후 추가
}


def get_policy(name: str, **kwargs) -> SchedulingPolicy:
    try:
        cls = _POLICIES[name]
    except KeyError:
        raise ValueError(f"unknown policy: {name} (available: {sorted(_POLICIES)})") from None
    return cls(**kwargs)
