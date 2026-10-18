from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


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
class ManualLocation:
    """A user-added place for a topic: a URL or a folder path. Link only."""

    id: int
    topic_name: str
    label: str
    target: str
    kind: str

    @property
    def display_label(self) -> str:
        cleaned = self.label.strip()
        if cleaned:
            return cleaned
        target = self.target.strip()
        if self.kind == "file":
            name = Path(target.rstrip("/\\")).name
            return name or target
        return urlparse(target).netloc or target

    @property
    def source_label(self) -> str:
        return "Folder" if self.kind == "file" else "Link"


@dataclass
class Topic:
    name: str
    locations: list[TopicLocation] = field(default_factory=list)
    manual_links: list[ManualLink] = field(default_factory=list)
    manual_locations: list[ManualLocation] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    onenote_paths: list[str] = field(default_factory=list)
    outlook_paths: list[str] = field(default_factory=list)
    plm_paths: list[str] = field(default_factory=list)
    states: set[str] = field(default_factory=set)
    display_state: str = ""
    has_inconsistency: bool = False
    pinned_rank: int | None = None
    hub_key: str | None = None
