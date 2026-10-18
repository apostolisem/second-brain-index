from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

TAG_FIELD = "tag"

FIELD_NAME = "name"
FIELD_TAGS = "tags"
FIELD_BOOKMARKS = "bookmarks"
FIELD_LOCATIONS = "locations"
FIELD_FILENAMES = "filenames"

MATCH_FIELD_LABELS = {
    FIELD_NAME: "name",
    FIELD_TAGS: "tags",
    FIELD_BOOKMARKS: "bookmarks",
    FIELD_LOCATIONS: "locations",
    FIELD_FILENAMES: "filenames",
}


@dataclass(frozen=True)
class SearchToken:
    text: str
    field: str | None = None
    # Set for a quoted ``tag:"…"`` value: the tag must equal the text.
    exact: bool = False


def parse_search_query(query: str) -> list[SearchToken]:
    tokens: list[SearchToken] = []
    for raw in _split_query(query):
        field = None
        text = raw
        prefix = f"{TAG_FIELD}:"
        if raw.casefold().startswith(prefix):
            field = TAG_FIELD
            text = raw[len(prefix):]
        text = text.strip()
        exact = field == TAG_FIELD and len(text) > 1 and text[0] == text[-1] == '"'
        text = text.strip('"')
        if not text:
            continue
        tokens.append(SearchToken(text=text.casefold(), field=field, exact=exact))
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

    The tags blob holds one tag per line. A ``tag:`` token is tested against
    each tag on its own, so it never matches across two tags; an ``exact``
    token must equal a whole tag.
    """
    matched_fields: set[str] = set()
    for token in tokens:
        if token.field == TAG_FIELD:
            tags = field_blobs.get(FIELD_TAGS, "").split("\n")
            if token.exact:
                matched = token.text in tags
            else:
                matched = any(token.text in tag for tag in tags)
            token_fields = {FIELD_TAGS} if matched else set()
        else:
            token_fields = {
                field
                for field in field_blobs
                if token.text in field_blobs.get(field, "")
            }
        if not token_fields:
            return None
        matched_fields |= token_fields
    return matched_fields


def rank_filename_hits(names: Sequence[str], tokens: list[SearchToken]) -> list[int]:
    """Return indexes of ``names`` containing at least one plain token.

    Names matching more tokens come first, then alphabetical. ``tag:`` tokens
    never match filenames.
    """
    texts = [token.text for token in tokens if token.field is None]
    if not texts:
        return []
    scored: list[tuple[int, str, int]] = []
    for index, name in enumerate(names):
        folded = name.casefold()
        score = sum(1 for text in texts if text in folded)
        if score:
            scored.append((-score, folded, index))
    scored.sort()
    return [index for _score, _folded, index in scored]


def filename_match_spans(
    name: str, tokens: Sequence[SearchToken]
) -> list[tuple[int, int]]:
    """Return the ``(start, end)`` ranges of ``name`` that plain tokens match.

    The ranges are sorted and merged, so none overlap or touch. ``tag:`` tokens
    never match filenames.
    """
    folded = name.casefold()
    if len(folded) != len(name):
        # Casefolding can lengthen a name ("ß" becomes "ss"), which would shift
        # every offset after it.
        folded = name.lower()
        if len(folded) != len(name):
            return []
    spans: list[tuple[int, int]] = []
    for text in {token.text for token in tokens if token.field is None}:
        start = folded.find(text)
        while start != -1:
            spans.append((start, start + len(text)))
            start = folded.find(text, start + 1)
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def format_match_fields(fields: set[str]) -> str:
    labels = [
        MATCH_FIELD_LABELS[field]
        for field in (FIELD_NAME, FIELD_TAGS, FIELD_BOOKMARKS, FIELD_LOCATIONS, FIELD_FILENAMES)
        if field in fields
    ]
    return ", ".join(labels)
