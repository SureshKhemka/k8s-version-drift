"""Version normalisation (FR-7).

Accepts ``v1.31.4``, ``1.31.4``, ``v1.31.4-eks-abc123``, ``v1.30.5-gke.1200``, ``v1.29.3+k3s1``.
Anything without a full ``major.minor.patch`` core is unparseable; the raw string is always kept.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering

_VERSION = re.compile(
    r"^v?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)(?:[-+][0-9A-Za-z.+-]*)?$"
)


@total_ordering
@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int

    @property
    def minor_key(self) -> str:
        """``"1.31"``"""
        return f"{self.major}.{self.minor}"

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return (self.major, self.minor, self.patch) < (other.major, other.minor, other.patch)


@dataclass(frozen=True)
class Minor:
    major: int
    minor: int

    @classmethod
    def parse(cls, text: str) -> Minor:
        m = re.fullmatch(r"(\d+)\.(\d+)", text.strip())
        if not m:
            raise ValueError(f"not a minor version: {text!r}")
        return cls(int(m.group(1)), int(m.group(2)))

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}"


@dataclass(frozen=True)
class VersionReading:
    """A reported version string plus its normalised form (``None`` when unparseable)."""

    raw: str
    parsed: Version | None

    @property
    def ok(self) -> bool:
        return self.parsed is not None


def parse_version(raw: str | None) -> VersionReading:
    text = (raw or "").strip()
    m = _VERSION.match(text)
    if not m:
        return VersionReading(raw=raw or "", parsed=None)
    return VersionReading(raw=text, parsed=Version(int(m["major"]), int(m["minor"]), int(m["patch"])))
