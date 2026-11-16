from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Sighting:
    fingerprint: str
    kind: str
    source: str
    repo: str
    signature: str
    count: int
    evidence: str
    policies: str
