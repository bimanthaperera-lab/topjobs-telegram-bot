"""Shared data types."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Job:
    url: str
    title: str
    company: str | None = None
    location: str | None = None
    posted: str | None = None
    description: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        return cls(
            url=data["url"],
            title=data["title"],
            company=data.get("company"),
            location=data.get("location"),
            posted=data.get("posted"),
            description=data.get("description"),
        )
