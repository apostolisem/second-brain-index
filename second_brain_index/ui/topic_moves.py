from __future__ import annotations

import os
from pathlib import Path

from PyQt6.QtWidgets import QDialog, QMessageBox

from ..constants import (
    OTHER_STATE,
    STATE_FOLDERS,
    hub_key_prefix,
    is_other_key,
    make_hub_key,
)
from ..indexer import SOURCE_SPECS
from ..models import Topic, TopicLocation
from .dialogs.operation_confirm import ConfirmOption, OperationConfirmDialog
from .dialogs.rename_topic_dialog import RenameTopicDialog
from .operations import (
    KEEP_SECTIONS,
    MoveRecord,
    UndoAborted,
    clean_manual_action_text,
    merge_conflict_dirname,
    prefixed_name,
)


class TopicMovesMixin:
    """Rename, merge, move and archive for MainWindow, and undoing the last of them.

    The planning and the JSON and folder work are TopicOperationsMixin's."""

    def rename_topic(self) -> None:
        if not self.current_topic_key:
            return
        if is_other_key(self.current_topic_key):
            return
        topic = self.topics_by_key.get(self.current_topic_key)
        if not topic:
            return

        old_key = self.current_topic_key
        old_name = topic.name

        dialog = RenameTopicDialog(
            self,
            old_name,
            lambda value, section: self.build_rename_preview(
                old_key, topic, old_name, value, section
            ),
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        preview = dialog.preview()
        if preview is None or preview.error:
            return

        new_name = preview.new_name
        new_key = preview.new_key
        operations = preview.operations
        merging = preview.is_merge
        verb = "Merge" if merging else "Rename"

        topic_hub = self.hub_for_topic(topic)
        json_targets, json_errors = self.prepare_json_rename_targets(topic_hub)
        if json_errors:
            message = [
                f"{verb} aborted because JSON sources could not be read:",
                *json_errors,
            ]
            QMessageBox.warning(self, f"{verb} failed", "\n".join(message))
            return

        rename_errors, completed, moves = self.execute_rename_operations(
            operations, merge_conflict_dirname(old_name)
        )
        if rename_errors:
            QMessageBox.warning(self, f"{verb} failed", "\n".join(rename_errors))
            return

        landing_state = preview.target_state
        if landing_state == KEEP_SECTIONS:
            landing_state = None
        json_errors, written_targets = self.apply_json_renames(
            json_targets, old_name, new_name, landing_state
        )
        if json_errors:
            rollback_errors = self.rollback_rename_operations(completed)
            for error in rollback_errors:
                json_errors.append(f"Rollback failed: {error}")
            message = [f"{verb} aborted; changes were rolled back.", *json_errors]
            QMessageBox.warning(self, f"{verb} failed", "\n".join(message))
            return

        migrated_link_ids = self.link_store.link_ids_for_topic(old_key)
        migrated_location_ids = self.link_store.location_ids_for_topic(old_key)
        pin_ranks = self.link_store.list_pinned()
        standalone_before = self.link_store.list_standalone_topics()
        tags_before = self.link_store.list_topic_tags()
        old_tags = list(tags_before.get(old_key, []))
        new_tags = list(tags_before.get(new_key, []))
        try:
            # One transaction: a failure part-way must not leave the bookmarks
            # under the new key while the folders go back to the old name.
            with self.link_store.transaction():
                self.link_store.reassign_topic_key(old_key, new_key)
                if old_key in self.hubs_by_project_key:
                    # Renaming a sub-hub project shifts every child topic key.
                    self.link_store.reassign_topic_prefix(
                        hub_key_prefix(old_key), hub_key_prefix(new_key)
                    )
                removed_links = self.link_store.dedupe_manual_links(
                    new_key, migrated_link_ids
                )
                self.link_store.reassign_locations_by_id(
                    migrated_location_ids, new_key
                )
                removed_locations = self.link_store.dedupe_manual_locations(
                    new_key, migrated_location_ids
                )
        except Exception as exc:
            rollback_errors = self.restore_json_targets(written_targets)
            rollback_errors.extend(self.rollback_rename_operations(completed))
            message = [f"Topic metadata update failed: {exc}"]
            for error in rollback_errors:
                message.append(f"Rollback failed: {error}")
            QMessageBox.warning(self, f"{verb} failed", "\n".join(message))
            return

        # Folders are already renamed on disk; a failure here must not roll back.
        tagged_previous_name = False
        # Undo must not strip a tag that was there before this operation.
        already_tagged = old_name.casefold() in {
            tag.casefold() for tag in (*old_tags, *new_tags)
        }
        if self.should_tag_previous_name(old_name, new_name):
            tag_error = self.record_previous_name_tag(new_key, old_name)
            if tag_error:
                QMessageBox.warning(
                    self,
                    verb,
                    f"{verb}d to '{new_name}', but the previous name was not "
                    f"saved as a tag.\n{tag_error}",
                )
            else:
                tagged_previous_name = not already_tagged

        journalled = self.journal_rename(
            kind="merge" if merging else "rename",
            old_key=old_key,
            new_key=new_key,
            old_name=old_name,
            new_name=new_name,
            moves=moves,
            boundaries=[op.target for op in completed if op.kind == "merge"],
            json_targets=written_targets,
            migrated_link_ids=migrated_link_ids,
            removed_links=removed_links,
            migrated_location_ids=migrated_location_ids,
            removed_locations=removed_locations,
            tagged_previous_name=tagged_previous_name,
            old_pin_rank=pin_ranks.get(old_key),
            new_pin_rank=pin_ranks.get(new_key),
            old_standalone_state=standalone_before.get(old_key),
            new_standalone_state=standalone_before.get(new_key),
            old_tags=old_tags,
            new_tags=new_tags,
            hub_renamed=old_key in self.hubs_by_project_key,
        )

        self.current_topic_key = new_key
        self.refresh_index()
        if journalled:
            action = (
                f"Merged '{old_name}' into '{new_name}'"
                if merging
                else f"Renamed '{old_name}' to '{new_name}'"
            )
            self.show_undo_prompt(action, "merge" if merging else "rename")

    def undo_last_operation(self) -> None:
        record = self.link_store.latest_operation()
        if record is None:
            self.clear_undo_prompt()
            self.statusBar().showMessage("Nothing to undo", 4000)
            return
        _record_id, kind, payload = record
        label = kind if kind in {"merge", "move"} else "rename"
        old_name = str(payload.get("old_name", ""))
        new_name = str(payload.get("new_name", ""))
        # Moves describe themselves; renames and merges predate that.
        what = str(
            payload.get("description")
            or f"the {label} of '{old_name}' into '{new_name}'"
        )
        blockers = self.undo_blockers(payload)
        if blockers:
            self.link_store.clear_operations()
            self.clear_undo_prompt()
            QMessageBox.warning(
                self,
                f"Undo {label} unavailable",
                "\n".join(
                    [
                        f"{what[0].upper()}{what[1:]} can no longer be undone;"
                        " nothing was changed.",
                        *blockers,
                    ]
                ),
            )
            return
        details = [f"{len(payload.get('moves', []))} file/folder move(s) will be reversed."]
        if kind == "move":
            details.append("JSON source lists, sections and renamed names will be restored.")
        else:
            details.append("JSON source lists and bookmarks will be restored.")
        if not self.confirm_destructive_operation(
            f"Undo {label}", f"Undo {what}?", details=details, accept_label="Undo"
        ):
            return

        try:
            if kind == "move":
                errors = self.apply_move_undo(payload)
            else:
                errors = self.apply_undo(payload)
        except UndoAborted as aborted:
            # The journal stays: once whatever holds the folders lets go, the
            # same undo can be tried again.
            QMessageBox.warning(
                self,
                f"Undo {label} failed",
                "\n".join(
                    [
                        "No folder could be moved back, so nothing else was "
                        "changed. Undo is still available.",
                        *aborted.errors,
                    ]
                ),
            )
            return
        self.link_store.clear_operations()
        self.clear_undo_prompt()
        if kind == "move":
            old_keys = {new_key: old_key for old_key, new_key, _hub in payload.get("renames", [])}
            self.current_topic_key = old_keys.get(self.current_topic_key, self.current_topic_key)
        else:
            self.current_topic_key = str(payload.get("old_key") or "") or None
        self.refresh_index()
        if errors:
            QMessageBox.warning(
                self,
                f"Undo {label} completed with warnings",
                "\n".join(errors),
            )
        else:
            self.statusBar().showMessage(f"Undid {what}", 5000)

    def archive_selected_topics(self) -> None:
        topics = self.get_selected_topics()
        if topics:
            self.archive_topics(topics)

    def archive_topics(self, topics: list[tuple[str, Topic]]) -> None:
        self.move_topics(topics, "Archive")

    def confirm_destructive_operation(
        self,
        title: str,
        text: str,
        details: list[str],
        manual_actions: list[str] | None = None,
        merge_conflicts: list[str] | None = None,
        accept_label: str = "Continue",
        option: ConfirmOption | None = None,
    ) -> bool:
        """Ask before an operation; a ticked ``option`` reads back as checked."""
        dialog = OperationConfirmDialog(
            self,
            title,
            text,
            details=details,
            manual_actions=manual_actions or [],
            merge_conflicts=merge_conflicts or [],
            accept_label=accept_label,
            option=option,
        )
        return dialog.exec() == QDialog.DialogCode.Accepted

    def report_operation_warnings(
        self,
        verb: str,
        resolve_errors: list[str],
        move_errors: list[str],
        merge_conflicts: list[str],
        json_update_errors: list[str],
    ) -> None:
        if not (resolve_errors or move_errors or merge_conflicts or json_update_errors):
            return
        message_parts = []
        if resolve_errors:
            message_parts.append(
                "Could not resolve destination for:\n" + "\n".join(resolve_errors)
            )
        if move_errors:
            message_parts.append("Failed to move:\n" + "\n".join(move_errors))
        if merge_conflicts:
            message_parts.append(
                "Conflicting items kept in a 'Merged from …' subfolder:\n"
                + "\n".join(merge_conflicts)
            )
        if json_update_errors:
            message_parts.append(
                "Failed to update JSON sources:\n" + "\n".join(json_update_errors)
            )
        QMessageBox.warning(
            self,
            f"{verb} completed with warnings",
            "\n\n".join(message_parts),
        )

    def move_topics(
        self, topics: list[tuple[str, Topic]], target_state: str
    ) -> None:
        target_label = STATE_FOLDERS[target_state]
        archiving = target_state == "Archive"
        action_word = "Archive" if archiving else "Move"
        eligible: list[tuple[str, Topic]] = []
        # Nothing on disk or in a list to move: only their stored section changes.
        standalone_keys: list[str] = []
        for key, topic in topics:
            if is_other_key(key):
                continue
            if topic.standalone:
                if topic.display_state != target_state:
                    eligible.append((key, topic))
                    standalone_keys.append(key)
                continue
            has_active_locations = any(
                loc.state not in {target_state, OTHER_STATE}
                for loc in topic.locations
            )
            # A source whose state cannot be inferred still needs updating.
            has_json_active = any(
                not states or states != {target_state}
                for _name, states in self.get_json_source_states(topic)
            )
            if has_active_locations or has_json_active:
                eligible.append((key, topic))
        if not eligible:
            QMessageBox.information(
                self,
                f"Nothing to {action_word.lower()}",
                f"The selected topic(s) are already in '{target_label}'.",
            )
            return

        manual_actions: list[str] = []
        for _key, topic in eligible:
            actions = self.collect_manual_move_actions(topic, target_state)
            if actions:
                if len(eligible) > 1:
                    manual_actions.extend(
                        f"{topic.name}: {clean_manual_action_text(action)}"
                        for action in actions
                    )
                else:
                    manual_actions.extend(actions)

        json_targets, json_errors = self.prepare_json_archive_targets(
            [topic for _key, topic in eligible]
        )
        if json_errors:
            message = [
                f"{action_word} aborted because JSON sources could not be read:",
                *json_errors,
            ]
            QMessageBox.warning(self, f"{action_word} failed", "\n".join(message))
            return

        operations: list[tuple[TopicLocation, Path]] = []
        operation_keys: list[str] = []
        resolve_errors: list[str] = []
        for key, topic in eligible:
            for location in topic.locations:
                if location.state in {target_state, OTHER_STATE}:
                    continue
                destination = self.resolve_move_destination(location, target_state)
                if not destination:
                    resolve_errors.append(str(location.path))
                    continue
                operations.append((location, destination))
                operation_keys.append(key)

        if not operations and not json_targets and not standalone_keys:
            QMessageBox.warning(
                self,
                f"{action_word} failed",
                "No valid move destinations were found for the selected topic(s).",
            )
            return

        topic_names = [topic.name for _key, topic in eligible]
        if operations:
            confirm_text = (
                f"{action_word} {len(topic_names)} topic(s): move {len(operations)} "
                f"folder(s) to '{target_label}'? This will move data on disk."
            )
            if json_targets:
                confirm_text += " JSON sources will be updated."
        else:
            confirm_text = f"Move {len(topic_names)} topic(s) to '{target_label}'"
            confirm_text += " in the JSON sources?" if json_targets else "?"

        details = self.describe_operations(operations, json_targets, topic_names)
        renames = self.prefix_renames(eligible, target_state)
        rename_option = None
        if renames:
            if len(renames) == 1:
                [(old_key, new_name)] = renames.items()
                label = f"Also rename '{self.topics_by_key[old_key].name}' to '{new_name}'"
            else:
                label = f"Also rename {len(renames)} topics to match the section"
                details = [*details, "", "Renamed if ticked:"] + [
                    f"  {self.topics_by_key[key].name} → {name}" for key, name in renames.items()
                ]
            rename_option = ConfirmOption(label)
        conflicts = [op for op in operations if op[1].exists()]
        if not self.confirm_destructive_operation(
            f"{action_word} topic(s)",
            confirm_text,
            details,
            manual_actions=manual_actions,
            merge_conflicts=[str(destination) for _loc, destination in conflicts],
            accept_label=action_word,
            option=rename_option,
        ):
            return
        if rename_option is None or not rename_option.checked:
            renames = {}
        if renames:
            operations = [
                (location, destination.with_name(renames[key]) if key in renames else destination)
                for key, (location, destination) in zip(operation_keys, operations)
            ]
            # A folder already in the target section is renamed where it is.
            for key, topic in eligible:
                for location in topic.locations:
                    if key in renames and location.state == target_state:
                        operations.append((location, location.path.with_name(renames[key])))
                        operation_keys.append(key)

        standalone_before = self.link_store.list_standalone_topics()
        record = MoveRecord()
        move_errors, merge_conflicts = self.execute_move_operations(operations, record)

        # A folder that is still in place was not moved; listing its topic
        # under the new section anyway would leave the two disagreeing.
        stuck = {
            key
            for key, (location, _destination) in zip(operation_keys, operations)
            if os.path.lexists(location.path)
        }
        moved = [(key, topic) for key, topic in eligible if key not in stuck]

        json_update_errors = [
            f"Left unchanged for '{topic.name}': its folder could not be moved."
            for key, topic in eligible
            if key in stuck and any(spec.paths_of(topic) for spec in SOURCE_SPECS)
        ]
        # Every list the move wrote, with its text before the move and after.
        written_lists: dict[Path, list[str]] = {}
        # Read again rather than reusing what the confirmation dialog showed:
        # the lists may have been edited while it was open. Renamed topics
        # are moved there along with their rename, below.
        json_targets, json_errors = self.prepare_json_archive_targets(
            [topic for key, topic in moved if key not in renames]
        )
        json_update_errors.extend(json_errors)
        if json_targets:
            update_errors, written = self.apply_json_moves(json_targets, target_state)
            json_update_errors.extend(update_errors)
            self._note_written_lists(written_lists, written)

        for key in standalone_keys:
            self.link_store.set_standalone_topic(key, target_state)

        renamed: list[list[object]] = []
        for key, topic in moved:
            if key in renames:
                json_update_errors.extend(
                    self._rename_moved_topic(
                        key, topic, renames[key], target_state, written_lists, renamed
                    )
                )
        new_keys = {old_key: new_key for old_key, new_key, _hub in renamed}
        if self.current_topic_key in new_keys:
            self.current_topic_key = new_keys[self.current_topic_key]

        journalled = False
        if record.moves or written_lists or standalone_keys or renamed:
            journalled = self.journal_move(
                description=(
                    f"the {action_word.lower()} of '{topic_names[0]}' to '{target_label}'"
                    if len(topic_names) == 1
                    else f"the {action_word.lower()} of {len(topic_names)} topics to '{target_label}'"
                ),
                moves=record.moves,
                boundaries=record.boundaries,
                json_targets=list(written_lists.values()),
                standalone_states={key: standalone_before.get(key) for key in standalone_keys},
                renames=renamed,
            )

        self.report_operation_warnings(
            action_word, resolve_errors, move_errors, merge_conflicts,
            json_update_errors,
        )
        self.refresh_index()
        if archiving:
            status_message = f"Archived {len(moved)} topic(s)"
        else:
            status_message = f"Moved {len(moved)} topic(s) to '{target_label}'"
        if journalled:
            self.show_undo_prompt(status_message, action_word.lower())
        else:
            self.statusBar().showMessage(status_message, 4000)

    def prefix_renames(
        self, eligible: list[tuple[str, Topic]], target_state: str
    ) -> dict[str, str]:
        """Topics whose P/A/R prefix the move would make wrong, with the right name.

        Left out when the new name is taken, so the offer never turns into a
        merge the user did not ask for.
        """
        renames: dict[str, str] = {}
        for key, topic in eligible:
            new_name = prefixed_name(topic.name, target_state)
            if new_name is None or self.validate_topic_name(new_name):
                continue
            new_key = make_hub_key(topic.hub_key, new_name) if topic.hub_key else new_name
            if self.find_colliding_topic_key(new_key, key) is not None:
                continue
            renames[key] = new_name
        return renames

    def _rename_moved_topic(
        self,
        key: str,
        topic: Topic,
        new_name: str,
        target_state: str,
        written_lists: dict[Path, list[str]],
        renamed: list[list[object]],
    ) -> list[str]:
        """Rename a topic whose folders the move already put under the new name.

        Its list entries are renamed and moved in one write, and its bookmarks,
        tags and pin follow the new key. Returns what could not be done.
        """
        errors: list[str] = []
        targets, read_errors = self.prepare_json_rename_targets(self.hub_for_topic(topic))
        errors.extend(
            f"'{topic.name}' was not renamed in {error}; rename it there by hand."
            for error in read_errors
        )
        rename_errors, written = self.apply_json_renames(
            targets, topic.name, new_name, target_state
        )
        errors.extend(rename_errors)
        self._note_written_lists(written_lists, written)
        new_key = make_hub_key(topic.hub_key, new_name) if topic.hub_key else new_name
        hub_renamed = key in self.hubs_by_project_key
        try:
            with self.link_store.transaction():
                self.link_store.reassign_topic_key(key, new_key)
                if hub_renamed:
                    self.link_store.reassign_topic_prefix(
                        hub_key_prefix(key), hub_key_prefix(new_key)
                    )
        except Exception as exc:
            errors.append(f"Bookmarks and tags of '{topic.name}' stayed on the old name: {exc}")
            return errors
        renamed.append([key, new_key, hub_renamed])
        return errors

    def _note_written_lists(
        self,
        written_lists: dict[Path, list[str]],
        written: list[tuple[str, Path, object, str]],
    ) -> None:
        """Keep each list's first original text and its latest written one."""
        for label, path, data, original in written:
            text = self.serialize_json_target(data)
            if path in written_lists:
                written_lists[path][3] = text
            else:
                written_lists[path] = [label, str(path), original, text]

    def journal_move(self, **payload: object) -> bool:
        payload["moves"] = [[str(source), str(target)] for source, target in payload["moves"]]
        payload["boundaries"] = [str(path) for path in payload["boundaries"]]
        try:
            self.link_store.record_operation("move", payload)
        except Exception:
            # Same as a rename: the move stands, only its undo is lost.
            self.clear_undo_prompt()
            return False
        return True

    def _resolve_location_root(self, location: TopicLocation) -> Path | None:
        state_folder_names = set(STATE_FOLDERS.values())
        for parent in (location.path.parent, *location.path.parents):
            if parent.name in state_folder_names:
                return parent.parent

        if location.source == "obsidian":
            for hub in self.hubs_by_project_key.values():
                if not hub.obsidian_root:
                    continue
                try:
                    location.path.relative_to(hub.obsidian_root)
                except ValueError:
                    continue
                return hub.obsidian_root
            return self.config.root_hub.obsidian_root

        candidates = list(self.config.root_hub.para_roots)
        for hub in self.hubs_by_project_key.values():
            candidates.extend(hub.para_roots)
        for candidate in candidates:
            try:
                location.path.relative_to(candidate)
            except ValueError:
                continue
            return candidate
        return None
