from __future__ import annotations

from dataclasses import dataclass

TAG_FIELD = "tag"

FIELD_NAME = "name"
FIELD_TAGS = "tags"
FIELD_BOOKMARKS = "bookmarks"
FIELD_FILENAMES = "filenames"

MATCH_FIELD_LABELS = {
    FIELD_NAME: "name",
    FIELD_TAGS: "tags",
    FIELD_BOOKMARKS: "bookmarks",
    FIELD_FILENAMES: "filenames",
}


@dataclass(frozen=True)
class SearchToken:
    text: str
    field: str | None = None


def parse_search_query(query: str) -> list[SearchToken]:
    tokens: list[SearchToken] = []
    for raw in _split_query(query):
        field = None
        text = raw
        prefix = f"{TAG_FIELD}:"
        if raw.casefold().startswith(prefix):
            field = TAG_FIELD
            text = raw[len(prefix):]
        text = text.strip().strip('"')
        if not text:
            continue
        tokens.append(SearchToken(text=text.casefold(), field=field))
    return tokens


def _split_query(query: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    in_quotes = False
    for char in query:
        if char == '"':
            in_quotes = not in_quotes
            current.append(char)
            continue
        if char.isspace() and not in_quotes:
            if current:
                parts.append("".join(current))
                current = []
            continue
        current.append(char)
    if current:
        parts.append("".join(current))
    return parts


def match_topic(
    field_blobs: dict[str, str], tokens: list[SearchToken]
) -> set[str] | None:
    """Return the set of field names that contributed to the match.

    ``field_blobs`` maps field names to casefolded search blobs. Returns
    ``None`` when any token fails to match. Tokens with ``field`` set are
    restricted to that field's blob (``tag:`` tokens only match tags).
    """
    matched_fields: set[str] = set()
    for token in tokens:
        if token.field == TAG_FIELD:
            candidates = [FIELD_TAGS]
        else:
            candidates = list(field_blobs)
        token_fields = {
            field
            for field in candidates
            if token.text in field_blobs.get(field, "")
        }
        if not token_fields:
            return None
        matched_fields |= token_fields
    return matched_fields


def format_match_fields(fields: set[str]) -> str:
    labels = [
        MATCH_FIELD_LABELS[field]
        for field in (FIELD_NAME, FIELD_TAGS, FIELD_BOOKMARKS, FIELD_FILENAMES)
        if field in fields
    ]
    return ", ".join(labels)
