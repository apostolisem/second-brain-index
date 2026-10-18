from __future__ import annotations

import unicodedata

# Names must survive a vault that syncs between Linux and Windows, so both
# platforms enforce the intersection of the two rule sets rather than whatever
# the current filesystem happens to tolerate.
PORTABLE_INVALID_CHARS = '<>:"/\\|?*'

RESERVED_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{digit}" for digit in range(1, 10)}
    | {f"LPT{digit}" for digit in range(1, 10)}
)

MAX_NAME_BYTES = 255


def describe_character(character: str) -> str:
    if character.isprintable():
        return f"'{character}'"
    return f"control character U+{ord(character):04X}"


def validate_portable_name(name: str) -> str | None:
    """Return an error message when the name is unusable on Linux or Windows."""
    cleaned = name.strip()
    if not cleaned:
        return "Topic name cannot be empty."
    if cleaned in {".", ".."}:
        return "Topic name cannot be '.' or '..'."

    for character in cleaned:
        if character in PORTABLE_INVALID_CHARS or ord(character) < 32:
            return (
                f"Topic name cannot contain {describe_character(character)}; "
                "it is not allowed on Windows."
            )

    stem = cleaned.split(".", 1)[0].strip()
    if stem.upper() in RESERVED_DEVICE_NAMES:
        return f"'{stem}' is a reserved device name on Windows."

    # Trailing spaces are trimmed rather than rejected, matching what Windows
    # itself does silently; a trailing dot has no such normalization and would
    # leave the folder unreachable there.
    if cleaned.endswith("."):
        return "Topic name cannot end with a dot."

    if len(cleaned.encode("utf-8")) > MAX_NAME_BYTES:
        return f"Topic name cannot be longer than {MAX_NAME_BYTES} bytes."

    return None


def normalize_key(name: str) -> str:
    """Fold a topic name to the key used for collision detection.

    Folds case, because Windows filesystems are case-insensitive and the tags
    table is UNIQUE COLLATE NOCASE, and folds Unicode composition, because the
    same name typed on two machines can differ byte-wise but not visually.
    """
    return unicodedata.normalize("NFC", name).casefold()
