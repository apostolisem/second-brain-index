from __future__ import annotations

import argparse
import difflib
import re
import shutil
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from second_brain_index.aggregation import build_topic_index
from second_brain_index.config import AppConfig
from second_brain_index.constants import display_name_from_key, hub_key_prefix
from second_brain_index.onenote import OneNoteTopicsError, load_onenote_topics
from second_brain_index.outlook import OutlookTopicsError, load_outlook_topics
from second_brain_index.scanner import scan_other_folders, scan_para_roots
from second_brain_index.subhub import discover_subhubs


@dataclass(frozen=True)
class LinkRecord:
    id: int
    topic_name: str
    link_name: str
    url: str
    link_type: str


@dataclass(frozen=True)
class TopicChoice:
    key: str
    display_name: str
    score: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Retarget orphaned bookmarks to current topics."
    )
    parser.add_argument(
        "--env",
        default=".env",
        help="Path to the .env file (default: .env in current directory).",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Override the database path (otherwise uses DB_PATH from .env).",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List orphaned topics and exit without prompting.",
    )
    parser.add_argument(
        "--map",
        action="append",
        default=[],
        metavar="OLD=NEW",
        help="Retarget bookmarks from OLD topic key to NEW topic key. Repeatable.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply changes to the database (default: dry-run).",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip creating a backup of the database before applying changes.",
    )
    parser.add_argument(
        "--max-suggestions",
        type=int,
        default=5,
        help="Maximum suggestions to show per orphaned topic (default: 5).",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=0.45,
        help="Minimum match score for suggestions (default: 0.45).",
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=3,
        help="Number of sample bookmarks to show per orphaned topic (default: 3).",
    )
    return parser.parse_args()


def load_manual_links(db_path: Path) -> list[LinkRecord]:
    connection = sqlite3.connect(str(db_path))
    try:
        connection.row_factory = sqlite3.Row
        cursor = connection.execute(
            "SELECT id, topic_name, link_name, url, link_type FROM manual_links"
        )
        return [
            LinkRecord(
                id=row["id"],
                topic_name=row["topic_name"],
                link_name=row["link_name"],
                url=row["url"],
                link_type=row["link_type"],
            )
            for row in cursor.fetchall()
        ]
    finally:
        connection.close()


def load_topics(config: AppConfig) -> dict[str, str]:
    filesystem_topics = scan_para_roots(config.root_hub.para_roots, "filesystem")
    obsidian_topics: dict[str, list] = {}
    if config.root_hub.obsidian_root:
        obsidian_topics = scan_para_roots([config.root_hub.obsidian_root], "obsidian")
    other_topics = scan_other_folders(config.other_folders, "filesystem")
    topics = build_topic_index(
        filesystem_topics=filesystem_topics,
        obsidian_topics=obsidian_topics,
        other_topics=other_topics,
        manual_links=[],
    )
    hubs, hub_errors = discover_subhubs(topics, config.root_hub)
    for key, message in hub_errors:
        print(f"Warning: sub-hub '{key}': {message}", file=sys.stderr)
    for project_key, hub in hubs.items():
        hub_obsidian: dict[str, list] = {}
        if hub.obsidian_root:
            hub_obsidian = scan_para_roots([hub.obsidian_root], "obsidian")
        topics.update(
            build_topic_index(
                filesystem_topics=scan_para_roots(hub.para_roots, "filesystem"),
                obsidian_topics=hub_obsidian,
                other_topics={},
                manual_links=[],
                key_prefix=hub_key_prefix(project_key),
                hub_key=project_key,
            )
        )
    topic_names = {key: topic.name for key, topic in topics.items()}

    if config.root_hub.onenote_enabled:
        try:
            onenote_topics = load_onenote_topics(config.root_hub.onenote_topics_path)
        except OneNoteTopicsError as exc:
            print(f"Warning: {exc}", file=sys.stderr)
        else:
            for name in onenote_topics:
                topic_names.setdefault(name, name)

    if config.root_hub.outlook_enabled:
        try:
            outlook_topics = load_outlook_topics(config.root_hub.outlook_topics_path)
        except OutlookTopicsError as exc:
            print(f"Warning: {exc}", file=sys.stderr)
        else:
            for name in outlook_topics:
                topic_names.setdefault(name, name)

    return topic_names


def normalize_label(value: str) -> str:
    lowered = value.casefold()
    lowered = re.sub(r"[_-]+", " ", lowered)
    lowered = re.sub(r"\s+", " ", lowered).strip()
    return lowered


def suggest_targets(
    orphan_key: str,
    topic_names: dict[str, str],
    max_suggestions: int,
    min_score: float,
) -> list[TopicChoice]:
    orphan_display = display_name_from_key(orphan_key)
    needle = normalize_label(orphan_display)
    choices: list[TopicChoice] = []
    for key, display in topic_names.items():
        score = difflib.SequenceMatcher(
            None, needle, normalize_label(display)
        ).ratio()
        if score >= min_score:
            choices.append(TopicChoice(key=key, display_name=display, score=score))
    choices.sort(key=lambda item: item.score, reverse=True)
    return choices[: max_suggestions]


def group_orphans(
    links: Iterable[LinkRecord], topic_keys: set[str]
) -> dict[str, list[LinkRecord]]:
    grouped: dict[str, list[LinkRecord]] = defaultdict(list)
    for link in links:
        if link.topic_name not in topic_keys:
            grouped[link.topic_name].append(link)
    return grouped


def print_orphans(orphans: dict[str, list[LinkRecord]]) -> None:
    print(f"Found {len(orphans)} orphaned topic(s).")
    for topic_key in sorted(orphans, key=str.casefold):
        display = display_name_from_key(topic_key)
        count = len(orphans[topic_key])
        print(f"- {display} (key: {topic_key}) -> {count} bookmark(s)")


def print_samples(links: list[LinkRecord], sample_size: int) -> None:
    for link in links[:sample_size]:
        print(f"  - {link.link_name} -> {link.url}")


def prompt_for_mapping(
    orphans: dict[str, list[LinkRecord]],
    topic_names: dict[str, str],
    max_suggestions: int,
    min_score: float,
    sample_size: int,
) -> dict[str, str]:
    mapping: dict[str, str] = {}
    topic_keys = set(topic_names)
    ordered_orphans = sorted(orphans.items(), key=lambda item: item[0].casefold())

    for orphan_key, links in ordered_orphans:
        display = display_name_from_key(orphan_key)
        print()
        print(f"Orphaned topic: {display} (key: {orphan_key})")
        print(f"Bookmarks: {len(links)}")
        if sample_size > 0:
            print("Samples:")
            print_samples(links, sample_size)

        suggestions = suggest_targets(
            orphan_key, topic_names, max_suggestions, min_score
        )
        if suggestions:
            print("Suggestions:")
            for idx, choice in enumerate(suggestions, start=1):
                score = f"{choice.score:.2f}"
                print(f"  {idx}) {choice.display_name} (key: {choice.key}, score: {score})")
        else:
            print("Suggestions: none")

        while True:
            raw = input(
                "Select target (number/key, ? to list topics, blank to skip): "
            ).strip()
            if not raw:
                break
            if raw == "?":
                print("Available topics:")
                for key in sorted(topic_keys, key=str.casefold):
                    display_name = topic_names[key]
                    print(f"- {display_name} (key: {key})")
                continue
            if raw.isdigit() and suggestions:
                index = int(raw)
                if 1 <= index <= len(suggestions):
                    mapping[orphan_key] = suggestions[index - 1].key
                    break
                print("Invalid selection.")
                continue
            normalized = resolve_topic_key(raw, topic_keys)
            if normalized:
                mapping[orphan_key] = normalized
                break
            print("Unknown topic key. Try again.")

    return mapping


def resolve_topic_key(value: str, topic_keys: set[str]) -> str | None:
    if value in topic_keys:
        return value
    matches = [key for key in topic_keys if key.casefold() == value.casefold()]
    if len(matches) == 1:
        return matches[0]
    return None


def parse_mapping_entries(entries: Iterable[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for entry in entries:
        if "=" not in entry:
            raise ValueError(f"Invalid mapping '{entry}'. Use OLD=NEW.")
        old, new = entry.split("=", 1)
        old = old.strip()
        new = new.strip()
        if not old or not new:
            raise ValueError(f"Invalid mapping '{entry}'. Use OLD=NEW.")
        mapping[old] = new
    return mapping


def apply_mapping(
    db_path: Path,
    mapping: dict[str, str],
    topic_keys: set[str],
) -> dict[str, int]:
    for old, new in mapping.items():
        if new not in topic_keys:
            raise ValueError(f"Target topic does not exist: {new}")
        if old == new:
            raise ValueError(f"Mapping uses the same key for old and new: {old}")

    counts: dict[str, int] = {}
    with sqlite3.connect(str(db_path)) as connection:
        for old, new in mapping.items():
            cursor = connection.execute(
                "UPDATE manual_links SET topic_name = ? WHERE topic_name = ?",
                (new, old),
            )
            counts[old] = cursor.rowcount
    return counts


def create_backup(db_path: Path) -> Path:
    backup_path = db_path.with_suffix(db_path.suffix + ".bak")
    suffix = 1
    while backup_path.exists():
        backup_path = db_path.with_suffix(db_path.suffix + f".bak{suffix}")
        suffix += 1
    shutil.copy2(db_path, backup_path)
    return backup_path


def main() -> int:
    args = parse_args()
    env_path = Path(args.env)
    config = AppConfig.load(env_path)
    db_path = Path(args.db) if args.db else config.db_path

    if args.max_suggestions < 0:
        print("--max-suggestions must be >= 0", file=sys.stderr)
        return 1
    if args.sample_size < 0:
        print("--sample-size must be >= 0", file=sys.stderr)
        return 1
    if not 0 <= args.min_score <= 1:
        print("--min-score must be between 0 and 1", file=sys.stderr)
        return 1

    if not env_path.exists():
        print(f"Warning: env file not found: {env_path}", file=sys.stderr)

    if not db_path.exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 1

    if not (config.root_hub.para_roots or config.root_hub.obsidian_root or config.other_folders):
        print(
            "No PARA roots, Obsidian vault, or other folders configured in .env.",
            file=sys.stderr,
        )
        return 1

    topic_names = load_topics(config)
    if not topic_names:
        print("No topics found from configured roots.", file=sys.stderr)
        return 1

    links = load_manual_links(db_path)
    orphans = group_orphans(links, set(topic_names))
    if not orphans:
        print("No orphaned bookmarks found.")
        return 0

    if args.list:
        print_orphans(orphans)
        return 0

    if args.map:
        try:
            mapping = parse_mapping_entries(args.map)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    else:
        if not sys.stdin.isatty():
            print("No mappings provided and input is not interactive.", file=sys.stderr)
            return 1
        mapping = prompt_for_mapping(
            orphans,
            topic_names,
            args.max_suggestions,
            args.min_score,
            args.sample_size,
        )

    if not mapping:
        print("No mappings selected. Nothing to do.")
        return 0

    invalid_sources = [key for key in mapping if key not in orphans]
    if invalid_sources:
        print(
            "Mapping references topic(s) without orphaned bookmarks: "
            + ", ".join(invalid_sources),
            file=sys.stderr,
        )
        return 1

    invalid_targets = sorted(
        {target for target in mapping.values() if target not in topic_names},
        key=str.casefold,
    )
    if invalid_targets:
        print(
            "Mapping references missing target topic(s): "
            + ", ".join(invalid_targets),
            file=sys.stderr,
        )
        return 1

    redundant_targets = sorted(
        {old for old, new in mapping.items() if old == new}, key=str.casefold
    )
    if redundant_targets:
        print(
            "Mapping uses the same key for old and new: "
            + ", ".join(redundant_targets),
            file=sys.stderr,
        )
        return 1

    print()
    print("Planned changes:")
    for old, new in mapping.items():
        count = len(orphans.get(old, []))
        old_display = display_name_from_key(old)
        new_display = topic_names.get(new, display_name_from_key(new))
        print(
            f"- {old_display} (key: {old}) -> {new_display} (key: {new}) "
            f"[{count} bookmark(s)]"
        )

    if not args.apply:
        print("Dry-run only. Re-run with --apply to update the database.")
        return 0

    if not args.no_backup:
        backup_path = create_backup(db_path)
        print(f"Backup created: {backup_path}")

    try:
        counts = apply_mapping(db_path, mapping, set(topic_names))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print("Update complete.")
    for old, count in counts.items():
        print(f"- {old}: {count} bookmark(s) updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
