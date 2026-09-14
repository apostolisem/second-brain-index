# Second Brain Hub

A desktop PyQt6 application for browsing and managing PARA-structured topics across filesystem roots and an Obsidian vault.

## Features

- Indexes direct topic folders under `01 - Projects`, `02 - Areas`, `03 - Resources`, and `04 - Archive` across multiple roots.
- Combines locations with the same topic name into one entry and flags topics found in multiple PARA states.
- Lists direct subfolders from additional roots in an **Others** section, with optional grouping by parent folder.
- Adds, edits, deletes, opens, searches, and moves bookmarks between topics.
- Adds, edits, removes, and searches topic tags.
- Pins topics above unpinned topics in the same section.
- Searches topic names, tags, bookmark names, bookmark titles, bookmark URLs, and optionally nested filenames. Quoted phrases and `tag:` filters are supported.
- Supports Ctrl/Shift multi-selection for moving, archiving, and tagging several topics.
- Moves topics between PARA sections. Existing destination folders are merged without overwriting files; conflicts are preserved under a `Merged from …` subfolder.
- Renames topics across their filesystem and Obsidian locations. Renaming to an existing topic merges their folders, bookmarks, tags, and pin state.
- Keeps the previous topic name as a tag after a rename, except for case-only renames.
- Can undo the latest successful rename or rename-based merge.
- Watches configured top-level folders for changes and refreshes the index automatically.
- Restores window geometry, splitter position, section expansion, filename-search preference, and the selected topic between sessions.

## Requirements

- Python 3.10 or later
- PyQt6 6.5 or later

## Installation

Install the Python dependency from the repository root:

```bash
python -m pip install -r requirements.txt
```

## Running

Run the application from the repository root:

```bash
python main.pyw
```

The window still opens when no topic roots are configured, but it displays a warning and has no folders to index.

## Keyboard shortcuts

| Shortcut | Action |
|---|---|
| `Ctrl+F` / `Ctrl+K` | Focus and select the search text |
| `Enter` / `Down` in search | Select the first visible topic |
| `F2` in the topic tree | Rename the selected topic |
| `F5` / `Ctrl+R` | Refresh the index |
| `Enter` in bookmarks | Open the selected bookmark |

Double-click a bookmark to open it. Double-click a tag to filter the topic tree by that tag.

## Project structure

```text
second-brain-hub/
├── main.pyw                          # Application entry point
├── retarget_orphaned_links.py        # Bookmark-retargeting CLI
├── requirements.txt                  # Python dependency
└── second_brain_index/
    ├── aggregation.py                # Topic aggregation and state resolution
    ├── config.py                     # Runtime configuration parsing
    ├── constants.py                  # PARA names and internal keys
    ├── db.py                         # Bookmark, tag, pin, and undo persistence
    ├── filename_index.py             # Background filename indexing
    ├── fsops.py                      # Safe rename and folder merge operations
    ├── models.py                     # Topic and bookmark models
    ├── naming.py                     # Portable topic-name validation
    ├── onenote.py                    # OneNote topic loading
    ├── outlook.py                    # Outlook topic loading
    ├── resources.py                  # Application resource lookup
    ├── scanner.py                    # Folder scanning
    ├── search.py                     # Search parsing and matching
    ├── subhub.py                     # Nested hub discovery
    ├── plm.py                        # PLM topic loading
    ├── ui.py                         # Main window and dialogs
    └── utils.py                      # Link-type detection
```

## PARA folder structure

Each configured PARA root is scanned for this layout:

```text
<root>/
├── 01 - Projects/
│   └── P - My Project/
├── 02 - Areas/
│   └── A - Health/
├── 03 - Resources/
│   └── R - Python/
└── 04 - Archive/
    └── P - Old Project/
```

Only direct subfolders of each PARA section become topics. A topic found in more than one root is shown once with all of its locations.

### Others grouping

Direct subfolders from additional roots appear under **Others**. Right-click the section header to toggle **Group by parent folder**. The grouping choice and group expansion state persist between sessions.

### Lifecycle inconsistency detection

A topic associated with multiple PARA states is marked `[!]` in amber. Its display section is chosen in this order:

1. The topic prefix: `P -` for Projects, `A -` for Areas, or `R -` for Resources.
2. The first available state in this order: Projects, Areas, Resources, Archive, Others.

## Bookmarks

The **Bookmarks** panel supports adding, editing, deleting, opening, and filtering bookmarks. Link types are inferred as web, file, email, Obsidian, another URL scheme, or other.

Select one or more bookmarks and use the context menu to move them to another topic or delete them in a batch. The topic picker filters its choices as you type.

## Tags

Tags are shared suggestions that can be attached independently to each topic. Editing a tag on one topic changes only that topic's association. Double-clicking a tag applies it as a search filter.

## Move and Rename

Move supports one or more selected topics. Rename operates on one topic.

### Move

Move sends eligible topic folders to Projects, Areas, Resources, or Archive. If the destination folder exists, contents are merged. Conflicting items are kept under `Merged from <topic name>` and reported after the operation.

Choosing Archive performs the same operation using `04 - Archive` as the destination.

### Rename

Rename validates names for Linux and Windows compatibility, then renames every eligible filesystem and Obsidian location. Case-only renames are supported.

If the new name already belongs to another topic, the dialog offers a merge. Cross-section merges can consolidate into one section or keep the existing sections. Existing destination items are never overwritten; conflicts are kept under `Merged from <old topic name>`.

After a rename, the old name is added as a tag so the topic remains searchable. The status bar offers an undo action for the latest rename or merge, restoring moved files and topic metadata where possible.

Topics in **Others** cannot be moved, archived, or renamed.

## Known limitations

- Filename data is rebuilt after a full refresh. Changes inside an existing topic may not trigger auto-refresh because nested files are not watched individually.
- Only the latest rename or rename-based merge can be undone. Ordinary Move and Archive operations are not undoable.
- File conflicts are preserved for manual review; the app does not choose which version should replace the other.

## License

Second Brain Hub is licensed under the [GNU General Public License v3.0](LICENSE).
