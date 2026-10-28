"""OneNote's topic list. Every list shares one format; see json_lists."""
from __future__ import annotations

from pathlib import Path

from .json_lists import TopicListError, infer_states_from_paths, load_topic_list

__all__ = ["OneNoteTopicsError", "infer_states_from_paths", "load_onenote_topics"]


class OneNoteTopicsError(TopicListError):
    pass


def load_onenote_topics(path: Path) -> dict[str, list[str]]:
    return load_topic_list(path, OneNoteTopicsError)
