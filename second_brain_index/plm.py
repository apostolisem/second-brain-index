"""PLM's topic list. Every list shares one format; see json_lists."""
from __future__ import annotations

from pathlib import Path

from .json_lists import TopicListError, infer_states_from_paths, load_topic_list

__all__ = ["PLMTopicsError", "infer_states_from_paths", "load_plm_topics"]


class PLMTopicsError(TopicListError):
    pass


def load_plm_topics(path: Path) -> dict[str, list[str]]:
    return load_topic_list(path, PLMTopicsError)
