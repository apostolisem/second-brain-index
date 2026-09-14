from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class TopicLocation:
    source: str
    state: str
    path: Path


@dataclass
class ManualLink:
    id: int
    topic_name: str
    link_name: str
    url: str
    link_type: str
    title: str = ""

    @property
    def searchable_title(self) -> str:
        cleaned = self.title.strip()
        return cleaned if cleaned else self.link_name


@dataclass
class Topic:
    name: str
    locations: list[TopicLocation] = field(default_factory=list)
    manual_links: list[ManualLink] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    onenote_paths: list[str] = field(default_factory=list)
    outlook_paths: list[str] = field(default_factory=list)
    plm_paths: list[str] = field(default_factory=list)
    states: set[str] = field(default_factory=set)
    display_state: str = ""
    has_inconsistency: bool = False
    pinned_rank: int | None = None
    hub_key: str | None = None
