from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .models import ManualLink


def escape_like(value: str) -> str:
    return (
        value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )


class ManualLinkStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.db_path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def _init_schema(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS manual_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_name TEXT NOT NULL,
                link_name TEXT NOT NULL,
                url TEXT NOT NULL,
                link_type TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT ''
            )
            """
        )
        self.connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_manual_links_topic
            ON manual_links(topic_name)
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS topic_tags (
                topic_name TEXT NOT NULL,
                tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                PRIMARY KEY (topic_name, tag_id)
            )
            """
        )
        self.connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_topic_tags_topic
            ON topic_tags(topic_name)
            """
        )
        self.connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_topic_tags_tag
            ON topic_tags(tag_id)
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pinned_topics (
                topic_name TEXT PRIMARY KEY,
                rank INTEGER NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS operation_journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
        self._ensure_title_column()
        self.connection.execute(
            """
            UPDATE manual_links
            SET title = link_name
            WHERE title IS NULL OR TRIM(title) = ''
            """
        )
        self.connection.commit()

    def _ensure_title_column(self) -> None:
        cursor = self.connection.execute("PRAGMA table_info(manual_links)")
        columns = {str(row["name"]) for row in cursor.fetchall()}
        if "title" not in columns:
            self.connection.execute(
                "ALTER TABLE manual_links ADD COLUMN title TEXT NOT NULL DEFAULT ''"
            )

    def list_links(self) -> list[ManualLink]:
        cursor = self.connection.execute(
            """
            SELECT id, topic_name, link_name, url, link_type, title
            FROM manual_links
            """
        )
        return [
            ManualLink(
                id=row["id"],
                topic_name=row["topic_name"],
                link_name=row["link_name"],
                url=row["url"],
                link_type=row["link_type"],
                title=(row["title"] or row["link_name"]),
            )
            for row in cursor.fetchall()
        ]

    def add_link(self, link: ManualLink) -> ManualLink:
        title = link.title.strip() or link.link_name
        cursor = self.connection.execute(
            """
            INSERT INTO manual_links (topic_name, link_name, url, link_type, title)
            VALUES (?, ?, ?, ?, ?)
            """,
            (link.topic_name, link.link_name, link.url, link.link_type, title),
        )
        self.connection.commit()
        return ManualLink(
            id=cursor.lastrowid,
            topic_name=link.topic_name,
            link_name=link.link_name,
            url=link.url,
            link_type=link.link_type,
            title=title,
        )

    def update_link(self, link: ManualLink) -> None:
        title = link.title.strip() or link.link_name
        self.connection.execute(
            """
            UPDATE manual_links
            SET link_name = ?, url = ?, link_type = ?, title = ?
            WHERE id = ?
            """,
            (link.link_name, link.url, link.link_type, title, link.id),
        )
        self.connection.commit()

    def delete_link(self, link_id: int) -> None:
        self.connection.execute("DELETE FROM manual_links WHERE id = ?", (link_id,))
        self.connection.commit()

    def reassign_link_topic(self, link_id: int, topic_name: str) -> None:
        self.connection.execute(
            "UPDATE manual_links SET topic_name = ? WHERE id = ?",
            (topic_name, link_id),
        )
        self.connection.commit()

    def list_topic_tags(self) -> dict[str, list[str]]:
        cursor = self.connection.execute(
            """
            SELECT tt.topic_name, t.name
            FROM topic_tags AS tt
            INNER JOIN tags AS t ON t.id = tt.tag_id
            ORDER BY tt.topic_name COLLATE NOCASE, t.name COLLATE NOCASE, t.name
            """
        )
        grouped: dict[str, list[str]] = defaultdict(list)
        for row in cursor.fetchall():
            grouped[str(row["topic_name"])].append(str(row["name"]))
        return dict(grouped)

    def list_tags_for_topic(self, topic_name: str) -> list[str]:
        cursor = self.connection.execute(
            """
            SELECT t.name
            FROM topic_tags AS tt
            INNER JOIN tags AS t ON t.id = tt.tag_id
            WHERE tt.topic_name = ?
            ORDER BY t.name COLLATE NOCASE, t.name
            """,
            (topic_name,),
        )
        return [str(row["name"]) for row in cursor.fetchall()]

    def list_tag_names(self) -> list[str]:
        cursor = self.connection.execute(
            """
            SELECT name
            FROM tags
            ORDER BY name COLLATE NOCASE, name
            """
        )
        return [str(row["name"]) for row in cursor.fetchall()]

    def add_topic_tag(self, topic_name: str, tag_name: str) -> bool:
        cleaned_name = self._normalize_tag_name(tag_name)
        with self.connection:
            tag_id = self._get_or_create_tag_id(cleaned_name)
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO topic_tags (topic_name, tag_id)
                VALUES (?, ?)
                """,
                (topic_name, tag_id),
            )
        return cursor.rowcount > 0

    def replace_topic_tag(
        self, topic_name: str, old_tag_name: str, new_tag_name: str
    ) -> bool:
        cleaned_old = self._normalize_tag_name(old_tag_name)
        cleaned_new = self._normalize_tag_name(new_tag_name)
        if cleaned_old.casefold() == cleaned_new.casefold():
            return False

        old_tag_id = self._get_tag_id(cleaned_old)
        if old_tag_id is None:
            return False

        with self.connection:
            cursor = self.connection.execute(
                """
                DELETE FROM topic_tags
                WHERE topic_name = ? AND tag_id = ?
                """,
                (topic_name, old_tag_id),
            )
            if cursor.rowcount == 0:
                return False
            new_tag_id = self._get_or_create_tag_id(cleaned_new)
            self.connection.execute(
                """
                INSERT OR IGNORE INTO topic_tags (topic_name, tag_id)
                VALUES (?, ?)
                """,
                (topic_name, new_tag_id),
            )
            self._delete_unused_tag(old_tag_id)
        return True

    def remove_topic_tag(self, topic_name: str, tag_name: str) -> bool:
        cleaned_name = self._normalize_tag_name(tag_name)
        tag_id = self._get_tag_id(cleaned_name)
        if tag_id is None:
            return False

        with self.connection:
            cursor = self.connection.execute(
                """
                DELETE FROM topic_tags
                WHERE topic_name = ? AND tag_id = ?
                """,
                (topic_name, tag_id),
            )
            if cursor.rowcount == 0:
                return False
            self._delete_unused_tag(tag_id)
        return True

    def list_pinned(self) -> dict[str, int]:
        cursor = self.connection.execute(
            "SELECT topic_name, rank FROM pinned_topics"
        )
        return {str(row["topic_name"]): int(row["rank"]) for row in cursor.fetchall()}

    def pin_topic(self, topic_name: str) -> int:
        cursor = self.connection.execute(
            "SELECT rank FROM pinned_topics WHERE topic_name = ?", (topic_name,)
        )
        row = cursor.fetchone()
        if row is not None:
            return int(row["rank"])
        cursor = self.connection.execute(
            "SELECT COALESCE(MAX(rank), 0) AS max_rank FROM pinned_topics"
        )
        rank = int(cursor.fetchone()["max_rank"]) + 1
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO pinned_topics (topic_name, rank)
                VALUES (?, ?)
                """,
                (topic_name, rank),
            )
        return rank

    def unpin_topic(self, topic_name: str) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                "DELETE FROM pinned_topics WHERE topic_name = ?", (topic_name,)
            )
        return cursor.rowcount > 0

    def reassign_topic_key(self, old_topic: str, new_topic: str) -> None:
        if old_topic == new_topic:
            return
        with self.connection:
            self.connection.execute(
                """
                UPDATE manual_links
                SET topic_name = ?
                WHERE topic_name = ?
                """,
                (new_topic, old_topic),
            )
            self.connection.execute(
                """
                UPDATE OR IGNORE topic_tags
                SET topic_name = ?
                WHERE topic_name = ?
                """,
                (new_topic, old_topic),
            )
            self.connection.execute(
                """
                DELETE FROM topic_tags
                WHERE topic_name = ?
                """,
                (old_topic,),
            )
            self.connection.execute(
                """
                UPDATE OR IGNORE pinned_topics
                SET topic_name = ?
                WHERE topic_name = ?
                """,
                (new_topic, old_topic),
            )
            self.connection.execute(
                "DELETE FROM pinned_topics WHERE topic_name = ?", (old_topic,)
            )

    def reassign_topic_prefix(self, old_prefix: str, new_prefix: str) -> None:
        if old_prefix == new_prefix:
            return
        pattern = escape_like(old_prefix) + "%"
        with self.connection:
            self.connection.execute(
                """
                UPDATE manual_links
                SET topic_name = ? || SUBSTR(topic_name, LENGTH(?) + 1)
                WHERE topic_name LIKE ? ESCAPE '\\'
                """,
                (new_prefix, old_prefix, pattern),
            )
            self.connection.execute(
                """
                UPDATE OR IGNORE topic_tags
                SET topic_name = ? || SUBSTR(topic_name, LENGTH(?) + 1)
                WHERE topic_name LIKE ? ESCAPE '\\'
                """,
                (new_prefix, old_prefix, pattern),
            )
            self.connection.execute(
                "DELETE FROM topic_tags WHERE topic_name LIKE ? ESCAPE '\\'",
                (pattern,),
            )
            self.connection.execute(
                """
                UPDATE OR IGNORE pinned_topics
                SET topic_name = ? || SUBSTR(topic_name, LENGTH(?) + 1)
                WHERE topic_name LIKE ? ESCAPE '\\'
                """,
                (new_prefix, old_prefix, pattern),
            )
            self.connection.execute(
                "DELETE FROM pinned_topics WHERE topic_name LIKE ? ESCAPE '\\'",
                (pattern,),
            )

    def link_ids_for_topic(self, topic_name: str) -> list[int]:
        cursor = self.connection.execute(
            "SELECT id FROM manual_links WHERE topic_name = ?", (topic_name,)
        )
        return [int(row["id"]) for row in cursor.fetchall()]

    def reassign_links_by_id(self, link_ids: list[int], topic_name: str) -> None:
        """Retarget only the given links, leaving the rest of the topic alone."""
        if not link_ids:
            return
        placeholders = ",".join("?" for _ in link_ids)
        with self.connection:
            self.connection.execute(
                f"UPDATE manual_links SET topic_name = ? WHERE id IN ({placeholders})",
                (topic_name, *link_ids),
            )

    def dedupe_manual_links(
        self, topic_name: str, deletable_ids: list[int]
    ) -> list[dict[str, object]]:
        """Drop bookmarks that a merge turned into duplicates.

        manual_links carries no unique constraint, so a merge can leave the same
        URL on the topic twice. The destination topic's own rows always win and
        only the caller's deletable rows (the ones the merge migrated) are
        removed, which makes the deletion reversible from the returned rows.
        """
        if not deletable_ids:
            return []
        deletable = set(deletable_ids)
        cursor = self.connection.execute(
            """
            SELECT id, link_name, url, link_type, title
            FROM manual_links
            WHERE topic_name = ?
            """,
            (topic_name,),
        )
        rows = [dict(row) for row in cursor.fetchall()]
        # Rows the destination already owned claim their signature first, so a
        # duplicate is always resolved in favour of the surviving topic.
        rows.sort(key=lambda row: (int(row["id"]) in deletable, int(row["id"])))

        seen: set[tuple[str, str]] = set()
        removed: list[dict[str, object]] = []
        removed_ids: list[int] = []
        for row in rows:
            signature = (str(row["url"]).strip(), str(row["link_name"]).strip())
            if signature not in seen:
                seen.add(signature)
                continue
            if int(row["id"]) not in deletable:
                continue
            removed_ids.append(int(row["id"]))
            removed.append(
                {
                    "link_name": row["link_name"],
                    "url": row["url"],
                    "link_type": row["link_type"],
                    "title": row["title"],
                }
            )
        if removed_ids:
            placeholders = ",".join("?" for _ in removed_ids)
            with self.connection:
                self.connection.execute(
                    f"DELETE FROM manual_links WHERE id IN ({placeholders})",
                    tuple(removed_ids),
                )
        return removed

    def restore_links(
        self, topic_name: str, rows: list[dict[str, object]]
    ) -> None:
        if not rows:
            return
        with self.connection:
            self.connection.executemany(
                """
                INSERT INTO manual_links (topic_name, link_name, url, link_type, title)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        topic_name,
                        row["link_name"],
                        row["url"],
                        row["link_type"],
                        row["title"],
                    )
                    for row in rows
                ],
            )

    def set_pin_rank(self, topic_name: str, rank: int | None) -> None:
        with self.connection:
            if rank is None:
                self.connection.execute(
                    "DELETE FROM pinned_topics WHERE topic_name = ?", (topic_name,)
                )
                return
            self.connection.execute(
                """
                INSERT INTO pinned_topics (topic_name, rank) VALUES (?, ?)
                ON CONFLICT(topic_name) DO UPDATE SET rank = excluded.rank
                """,
                (topic_name, rank),
            )

    def record_operation(self, kind: str, payload: dict[str, object]) -> int:
        """Store an undo record, replacing any older one.

        Only the most recent operation is undoable, so the table never holds
        more than a single row.
        """
        created_at = datetime.now(timezone.utc).isoformat()
        with self.connection:
            self.connection.execute("DELETE FROM operation_journal")
            cursor = self.connection.execute(
                """
                INSERT INTO operation_journal (created_at, kind, payload)
                VALUES (?, ?, ?)
                """,
                (created_at, kind, json.dumps(payload)),
            )
        return int(cursor.lastrowid)

    def latest_operation(self) -> tuple[int, str, dict[str, object]] | None:
        cursor = self.connection.execute(
            """
            SELECT id, kind, payload
            FROM operation_journal
            ORDER BY id DESC
            LIMIT 1
            """
        )
        row = cursor.fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["payload"])
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        return int(row["id"]), str(row["kind"]), payload

    def clear_operations(self) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM operation_journal")

    def rename_topic(self, old_topic: str, new_topic: str) -> int:
        cursor = self.connection.execute(
            "SELECT COUNT(*) AS count FROM manual_links WHERE topic_name = ?",
            (old_topic,),
        )
        row = cursor.fetchone()
        self.reassign_topic_key(old_topic, new_topic)
        if row is None:
            return 0
        return int(row["count"])

    def _normalize_tag_name(self, tag_name: str) -> str:
        cleaned = tag_name.strip()
        if not cleaned:
            raise ValueError("Tag name cannot be empty.")
        return cleaned

    def _get_tag_id(self, tag_name: str) -> int | None:
        cursor = self.connection.execute(
            """
            SELECT id
            FROM tags
            WHERE name = ? COLLATE NOCASE
            """,
            (tag_name,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return int(row["id"])

    def _get_or_create_tag_id(self, tag_name: str) -> int:
        existing = self._get_tag_id(tag_name)
        if existing is not None:
            return existing

        cursor = self.connection.execute(
            """
            INSERT INTO tags (name)
            VALUES (?)
            """,
            (tag_name,),
        )
        return int(cursor.lastrowid)

    def _delete_unused_tag(self, tag_id: int) -> None:
        cursor = self.connection.execute(
            """
            SELECT 1
            FROM topic_tags
            WHERE tag_id = ?
            LIMIT 1
            """,
            (tag_id,),
        )
        if cursor.fetchone() is not None:
            return
        self.connection.execute("DELETE FROM tags WHERE id = ?", (tag_id,))

    def close(self) -> None:
        self.connection.close()
