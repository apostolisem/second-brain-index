from __future__ import annotations

from collections import defaultdict

from .constants import PREFIX_TO_STATE, STATE_ORDER, display_name_from_key
from .models import ManualLink, Topic, TopicLocation


def build_topic_index(
    filesystem_topics: dict[str, list[TopicLocation]],
    obsidian_topics: dict[str, list[TopicLocation]],
    other_topics: dict[str, list[TopicLocation]],
    manual_links: list[ManualLink],
    key_prefix: str = "",
    hub_key: str | None = None,
) -> dict[str, Topic]:
    locations_by_topic: dict[str, set[TopicLocation]] = defaultdict(set)

    for name, locations in filesystem_topics.items():
        locations_by_topic[f"{key_prefix}{name}"].update(locations)
    for name, locations in obsidian_topics.items():
        locations_by_topic[f"{key_prefix}{name}"].update(locations)
    for name, locations in other_topics.items():
        locations_by_topic[f"{key_prefix}{name}"].update(locations)

    topics: dict[str, Topic] = {}
    for key, locations in locations_by_topic.items():
        display_name = display_name_from_key(key)
        location_list = sorted(
            locations, key=lambda loc: (loc.source, loc.state, str(loc.path))
        )
        states = {loc.state for loc in locations}
        has_inconsistency = len(states) > 1
        display_state = choose_display_state(display_name, states, has_inconsistency)
        topics[key] = Topic(
            name=display_name,
            locations=location_list,
            manual_links=[],
            states=states,
            display_state=display_state,
            has_inconsistency=has_inconsistency,
            hub_key=hub_key,
        )

    links_by_topic: dict[str, list[ManualLink]] = defaultdict(list)
    for link in manual_links:
        links_by_topic[link.topic_name].append(link)

    for key, topic in topics.items():
        links = links_by_topic.get(key, [])
        topic.manual_links = sorted(links, key=lambda link: link.link_name.casefold())

    return topics


def choose_display_state(
    topic_name: str, states: set[str], has_inconsistency: bool
) -> str:
    if not states:
        return STATE_ORDER[0]

    if has_inconsistency:
        for prefix, state in PREFIX_TO_STATE.items():
            if topic_name.startswith(prefix):
                return state

    for state in STATE_ORDER:
        if state in states:
            return state

    return STATE_ORDER[0]
