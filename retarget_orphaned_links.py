#!/usr/bin/env python3
from __future__ import annotations

import argparse
import difflib
import re
import shlex
import shutil
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from second_brain_index.aggregation import build_topic_index
from second_brain_index.config import AppConfig
from second_brain_index.constants import (
    display_name_from_key,
    hub_key_prefix,
    make_hub_key,
)
from second_brain_index.db import ManualLinkStore
from second_brain_index.indexer import SOURCE_SPECS
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
        "--delete",
        action="append",
        default=[],
        metavar="OLD",
        help=(
            "Delete the bookmarks, locations, tags and pin of orphaned topic "
            "OLD. Repeatable."
        ),
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
        "--allow-missing-roots",
        action="store_true",
        help=(
            "Run even when a configured root is not on disk. Every topic under "
            "it would then look orphaned."
        ),
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


def has_manual_locations(connection: sqlite3.Connection) -> bool:
    """Databases created before manual locations existed lack the table."""
    cursor = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'manual_locations'"
    )
    return cursor.fetchone() is not None


def load_manual_links(db_path: Path) -> list[LinkRecord]:
    """Bookmarks plus manual locations: both are keyed by topic name."""
    connection = sqlite3.connect(str(db_path))
    try:
        connection.row_factory = sqlite3.Row
        cursor = connection.execute(
            "SELECT id, topic_name, link_name, url, link_type FROM manual_links"
        )
        records = [
            LinkRecord(
                id=row["id"],
                topic_name=row["topic_name"],
                link_name=row["link_name"],
                url=row["url"],
                link_type=row["link_type"],
            )
            for row in cursor.fetchall()
        ]
        if has_manual_locations(connection):
            cursor = connection.execute(
                "SELECT id, topic_name, label, target, kind FROM manual_locations"
            )
            records.extend(
                LinkRecord(
                    id=row["id"],
                    topic_name=row["topic_name"],
                    link_name=row["label"] or row["target"],
                    url=row["target"],
                    link_type=f"location:{row['kind']}",
                )
                for row in cursor.fetchall()
            )
        return records
    finally:
        connection.close()


def load_topic_extras(db_path: Path) -> dict[str, list[str]]:
    """Tags and pin per topic key, described for display; a topic can be
    orphaned with only these and no links."""
    connection = sqlite3.connect(str(db_path))
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        tags: dict[str, list[str]] = defaultdict(list)
        if {"topic_tags", "tags"} <= tables:
            for topic_name, tag_name in connection.execute(
                "SELECT topic_tags.topic_name, tags.name FROM topic_tags"
                " JOIN tags ON tags.id = topic_tags.tag_id"
            ):
                tags[topic_name].append(tag_name)
        extras: dict[str, list[str]] = {
            key: ["tags: " + ", ".join(sorted(names, key=str.casefold))]
            for key, names in tags.items()
        }
        if "pinned_topics" in tables:
            for (topic_name,) in connection.execute(
                "SELECT topic_name FROM pinned_topics"
            ):
                extras.setdefault(topic_name, []).append("pinned")
        return extras
    finally:
        connection.close()


def describe_contents(links: list[LinkRecord], extras: list[str]) -> str:
    """What an orphaned key holds, e.g. "2 bookmarks, 1 location, pinned"."""
    locations = sum(link.link_type.startswith("location:") for link in links)
    bookmarks = len(links) - locations
    parts = [
        f"{count} {noun}{'' if count == 1 else 's'}"
        for count, noun in ((bookmarks, "bookmark"), (locations, "location"))
        if count
    ]
    return ", ".join(parts + extras) or "nothing"


def missing_roots(config: AppConfig) -> list[Path]:
    """Configured roots that are not a folder, such as an unmounted drive."""
    roots = [*config.root_hub.para_roots, *config.other_folders]
    if config.root_hub.obsidian_root is not None:
        roots.append(config.root_hub.obsidian_root)
    return [root for root in roots if not root.is_dir()]


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

    # Every list the app reads: a topic that lives only in one of them is not
    # an orphan, whichever source or sub-hub it belongs to.
    for hub_key, hub in ((None, config.root_hub), *hubs.items()):
        for spec in SOURCE_SPECS:
            if not spec.enabled(hub):
                continue
            try:
                source_topics = spec.loader(spec.topics_path(hub))
            except spec.error_class as exc:
                print(f"Warning: {exc}", file=sys.stderr)
                continue
            for name in source_topics:
                key = make_hub_key(hub_key, name) if hub_key else name
                topic_names.setdefault(key, name)

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
    links: Iterable[LinkRecord],
    topic_keys: set[str],
    other_keys: Iterable[str] = (),
) -> dict[str, list[LinkRecord]]:
    """Orphaned topic keys with their bookmarks; ``other_keys`` are keys that
    only carry tags or a pin, listed with no bookmarks."""
    grouped: dict[str, list[LinkRecord]] = defaultdict(list)
    for link in links:
        if link.topic_name not in topic_keys:
            grouped[link.topic_name].append(link)
    for key in other_keys:
        if key not in topic_keys:
            grouped.setdefault(key, [])
    return dict(grouped)


def print_orphans(
    orphans: dict[str, list[LinkRecord]], extras: dict[str, list[str]]
) -> None:
    print(f"Found {len(orphans)} orphaned topic(s).")
    for topic_key in sorted(orphans, key=str.casefold):
        display = display_name_from_key(topic_key)
        contents = describe_contents(orphans[topic_key], extras.get(topic_key, []))
        print(f"- {display} (key: {topic_key}) -> {contents}")


def print_samples(links: list[LinkRecord], sample_size: int) -> None:
    for link in links[:sample_size]:
        kind = "location" if link.link_type.startswith("location:") else "bookmark"
        print(f"  - [{kind}] {link.link_name} -> {link.url}")


def prompt_for_mapping(
    orphans: dict[str, list[LinkRecord]],
    topic_names: dict[str, str],
    max_suggestions: int,
    min_score: float,
    sample_size: int,
    extras: dict[str, list[str]],
) -> dict[str, str | None]:
    """Target key per orphaned key; None marks an orphan to delete."""
    mapping: dict[str, str | None] = {}
    topic_keys = set(topic_names)
    ordered_orphans = sorted(orphans.items(), key=lambda item: item[0].casefold())

    for orphan_key, links in ordered_orphans:
        display = display_name_from_key(orphan_key)
        print()
        print(f"Orphaned topic: {display} (key: {orphan_key})")
        print(f"Holds: {describe_contents(links, extras.get(orphan_key, []))}")
        if sample_size > 0 and links:
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
            print(
                "Suggestions: none; no topic has a similar name. Search with "
                "?text, or type d to delete it if you no longer need it."
            )

        while True:
            raw = input(
                "Target (number, topic name or key; ?text to search; "
                "d to delete; blank to skip; q to finish): "
            ).strip()
            if not raw:
                break
            if raw.casefold() == "q":
                return mapping
            if raw.casefold() == "d":
                mapping[orphan_key] = None
                break
            if raw.startswith("?"):
                print_topic_list(topic_names, raw[1:].strip())
                continue
            if raw.isdigit() and raw not in topic_keys:
                index = int(raw)
                if 1 <= index <= len(suggestions):
                    mapping[orphan_key] = suggestions[index - 1].key
                    break
                if suggestions:
                    print(f"Pick a number from 1 to {len(suggestions)}.")
                else:
                    print("There are no suggestions to pick from; type a topic name.")
                continue
            matches = resolve_topic_key(raw, topic_names)
            if len(matches) == 1:
                mapping[orphan_key] = matches[0]
                break
            if matches:
                print("Several topics match; type one of these keys:")
                for key in matches:
                    print(f"- {topic_names[key]} (key: {key})")
                continue
            print(f"No topic named '{raw}'. Use ?{raw} to search.")

    return mapping


def print_topic_list(topic_names: dict[str, str], needle: str) -> None:
    folded = needle.casefold()
    keys = [
        key
        for key in sorted(topic_names, key=str.casefold)
        if folded in key.casefold() or folded in topic_names[key].casefold()
    ]
    if not keys:
        print(f"No topics contain '{needle}'.")
        return
    print("Topics:")
    for key in keys:
        print(f"- {topic_names[key]} (key: {key})")


def resolve_topic_key(value: str, topic_names: dict[str, str]) -> list[str]:
    """Topic keys that match an exact key, else a key or name ignoring case.

    Sub-hub keys carry their project as a prefix, so the plain name is what a
    person types; it can match more than one key.
    """
    if value in topic_names:
        return [value]
    folded = value.casefold()
    return sorted(
        (
            key
            for key, name in topic_names.items()
            if key.casefold() == folded or name.casefold() == folded
        ),
        key=str.casefold,
    )


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
    mapping: dict[str, str | None],
    topic_keys: set[str],
) -> None:
    """Move each orphan to its target, or delete it when the target is None."""
    for old, new in mapping.items():
        if new is not None and new not in topic_keys:
            raise ValueError(f"Target topic does not exist: {new}")
        if old == new:
            raise ValueError(f"Mapping uses the same key for old and new: {old}")

    store = ManualLinkStore(db_path)
    try:
        # The app's own reassignment, so tags and pins follow the bookmarks;
        # all changes land together or not at all.
        with store.transaction():
            for old, new in mapping.items():
                if new is None:
                    store.forget_topic(old)
                else:
                    store.reassign_topic_key(old, new)
    finally:
        store.close()


def confirm(prompt: str) -> bool:
    try:
        return input(prompt).strip().casefold() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def rerun_command(mapping: dict[str, str | None]) -> str:
    choices = [
        f"--delete={old}" if new is None else f"--map={old}={new}"
        for old, new in mapping.items()
    ]
    return shlex.join(["python3", *sys.argv, "--apply", *choices])


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

    absent = missing_roots(config)
    if absent and not args.allow_missing_roots:
        print(
            "These configured roots are not on disk, so every topic under them "
            "would look orphaned:",
            file=sys.stderr,
        )
        for root in absent:
            print(f"- {root}", file=sys.stderr)
        print(
            "Mount them, or re-run with --allow-missing-roots.", file=sys.stderr
        )
        return 1

    topic_names = load_topics(config)
    if not topic_names:
        print("No topics found from configured roots.", file=sys.stderr)
        return 1

    links = load_manual_links(db_path)
    extras = load_topic_extras(db_path)
    orphans = group_orphans(links, set(topic_names), extras)
    if not orphans:
        print("No orphaned topics found.")
        return 0

    if args.list:
        print_orphans(orphans, extras)
        return 0

    mapping: dict[str, str | None]
    if args.map or args.delete:
        try:
            mapping = dict(parse_mapping_entries(args.map))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        both = sorted(set(mapping) & set(args.delete), key=str.casefold)
        if both:
            print(
                "Topic(s) both mapped and deleted: " + ", ".join(both),
                file=sys.stderr,
            )
            return 1
        mapping.update((key, None) for key in args.delete)
    else:
        if not sys.stdin.isatty():
            print("No mappings provided and input is not interactive.", file=sys.stderr)
            return 1
        try:
            mapping = prompt_for_mapping(
                orphans,
                topic_names,
                args.max_suggestions,
                args.min_score,
                args.sample_size,
                extras,
            )
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled. Nothing was changed.")
            return 1

    if not mapping:
        print("Nothing selected. Nothing was changed.")
        return 0

    invalid_sources = [key for key in mapping if key not in orphans]
    if invalid_sources:
        print(
            "Not an orphaned topic: "
            + ", ".join(invalid_sources),
            file=sys.stderr,
        )
        return 1

    invalid_targets = sorted(
        {
            target
            for target in mapping.values()
            if target is not None and target not in topic_names
        },
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
        contents = describe_contents(orphans.get(old, []), extras.get(old, []))
        old_display = display_name_from_key(old)
        if new is None:
            print(f"- {old_display} (key: {old}) -> delete [{contents}]")
            continue
        new_display = topic_names.get(new, display_name_from_key(new))
        print(
            f"- {old_display} (key: {old}) -> {new_display} (key: {new}) "
            f"[{contents}]"
        )

    if not args.apply:
        # Choices made at the prompt would be lost on a re-run, so offer to
        # apply them here instead of only reporting a dry run.
        if args.map or args.delete:
            print("Dry-run only. Re-run with --apply to update the database.")
            return 0
        if not confirm("Apply these changes now? [y/N] "):
            print("Dry-run only. Nothing was changed. To apply, run:")
            print("  " + rerun_command(mapping))
            return 0

    if not args.no_backup:
        backup_path = create_backup(db_path)
        print(f"Backup created: {backup_path}")

    try:
        apply_mapping(db_path, mapping, set(topic_names))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print("Update complete.")
    for old, new in mapping.items():
        print(f"- {old}: " + ("deleted" if new is None else f"moved to {new}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
