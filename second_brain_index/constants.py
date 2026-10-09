STATE_FOLDERS = {
    "Projects": "01 - Projects",
    "Areas": "02 - Areas",
    "Resources": "03 - Resources",
    "Archive": "04 - Archive",
}

OTHER_STATE = "Others"
OTHER_KEY_PREFIX = "__others__::"
HUB_KEY_PREFIX = "__hub__::"

STATE_ORDER = ["Projects", "Areas", "Resources", "Archive", OTHER_STATE]

PREFIX_TO_STATE = {
    "P -": "Projects",
    "A -": "Areas",
    "R -": "Resources",
}


def make_other_key(name: str) -> str:
    return f"{OTHER_KEY_PREFIX}{name}"


def is_other_key(key: str) -> bool:
    return key.startswith(OTHER_KEY_PREFIX)


def hub_key_prefix(project_key: str) -> str:
    return f"{HUB_KEY_PREFIX}{project_key}::"


def make_hub_key(project_key: str, topic_name: str) -> str:
    return f"{hub_key_prefix(project_key)}{topic_name}"


def is_hub_key(key: str) -> bool:
    return key.startswith(HUB_KEY_PREFIX)


def split_hub_key(key: str) -> tuple[str, str]:
    remainder = key[len(HUB_KEY_PREFIX) :]
    project_key, _, topic_name = remainder.partition("::")
    return project_key, topic_name


def display_name_from_key(key: str) -> str:
    if is_other_key(key):
        return key[len(OTHER_KEY_PREFIX) :]
    if is_hub_key(key):
        return split_hub_key(key)[1]
    return key
