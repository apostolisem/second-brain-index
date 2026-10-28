# Second Brain Hub

A desktop PyQt6 application for browsing and managing PARA-structured topics across filesystem roots and an Obsidian vault.

## Features

- Indexes direct topic folders under `01 - Projects`, `02 - Areas`, `03 - Resources`, and `04 - Archive` across multiple roots.
- Combines locations with the same topic name into one entry and flags topics found in multiple PARA states.
- Lists direct subfolders from additional roots in an **Others** section, with optional grouping by parent folder.
- Adds, edits, deletes, opens, searches, and moves bookmarks between topics.
- Adds, edits, removes, and searches topic tags.
- Pins topics above unpinned topics in the same section.
- Shows the topic's action buttons beside its title, and drops them onto their own row when the title is too long to fit on one line next to them.
- Searches topic names, tags, bookmark names, bookmark titles, bookmark URLs, the labels and targets of locations you added, and optionally nested filenames (including those inside folders you added). Quoted phrases and `tag:` filters are supported: `tag:word` matches any tag containing the word, `tag:"name"` matches that exact tag. Case and accents are ignored, so `cafe` finds `Café`.
- Lists the files and folders that match the active search for the selected topic, and opens them or their containing folder.
- Filters topics by location from the header button that reads **All locations** until you pick one: a source (Filesystem, Obsidian, OneNote, Outlook, PLM, an added folder or link), a configured root, a sub-hub's root, or topics with no folder on disk. The filter combines with search and Focus, limits filename matches and the matching-files list to that location, and starts at **All locations** each time the app opens.
- Supports Ctrl/Shift multi-selection: with two or more topics selected, the main area shows a panel for moving or tagging them all.
- Light and dark themes; the header toggle switches between them or follows the system.
- Moves topics between PARA sections. Existing destination folders are merged without overwriting files; conflicts are preserved under a `Merged from …` subfolder. When the move would leave a topic's `P -`, `A -` or `R -` prefix pointing at the wrong section, the confirmation offers to rename it to match.
- Renames topics across their filesystem and Obsidian locations. Renaming to an existing topic merges their folders, bookmarks, tags, and pin state.
- Keeps the previous topic name as a tag after a rename, except for case-only renames.
- Can undo the latest rename, merge, move or archive, as long as the folders and lists it changed have not been changed since.
- Watches configured top-level folders for changes and refreshes the index automatically.
- Scans in the background, so the window stays responsive on slow drives; a long scan shows a progress dialog with a Cancel button that keeps the previous index.
- Lists every location of a topic (filesystem, Obsidian, OneNote, Outlook, PLM) in one Locations panel; click a location to open it, right-click to open its containing folder or copy its path.
- Shows every location and bookmark of a topic without inner scrollbars (the details page scrolls), and highlights the whole row under the pointer.
- Gives a OneNote, Outlook, or PLM location a direct link from the row's pencil or right-click menu; until one is set, the row opens that source's home page.
- Manages the OneNote, Outlook, and PLM topic lists without opening their JSON files: add the selected topic from **Add location** (with an optional direct link), remove it by right-clicking its location, or create an entry that has no folder from **Sources → New … entry…**.
- Reads those lists even when a hand edit left a trailing comma behind, and creates a list that does not exist yet the first time a topic is added to it.
- Creates a topic from **New** (Ctrl+N), a short wizard: name, hub (the main hub or a sub-hub) and PARA section, Archive included, then where to create it. Filesystem and Obsidian folders are created by the app, the Obsidian one with a `README.md` saying so; OneNote, Outlook and PLM get an entry in their topic list, and the last page lists what to create in those apps yourself, with a button to open each. A new project in the main hub can be set up as a Sub-Hub from the same wizard. The same page adds links and folders of your own, as **Add location** does. They can be the whole topic: with no folder or list ticked, the topic is kept in the database with its section, can be moved, archived and renamed like any other, gives way to a folder or list entry of the same name once one appears, and goes when its last link or folder is removed. A name that already exists in that section only offers the places it is still missing.
- Creates a topic's missing Filesystem or Obsidian folder from **Add location**, at the path the New topic wizard would use (its hub, section and name; the Obsidian one with a `README.md`). A folder the topic already has in that root, in any section, is shown greyed out.
- Adds your own locations to a topic: a URL or a folder path, with an optional label, listed after the discovered ones.
- Restores window geometry, splitter position, section expansion, filename-search preference, and the selected topic between sessions, separately for each `.env`, so two checkouts do not share them. The theme is shared.
- Lists what is stored under topics that no longer exist (bookmarks, links and folders, tags, pins) in **Sources → Orphaned data…**, to reattach to another topic, bring back as a links-only topic, or delete.

## Requirements

- Python 3.10 or later
- PyQt6 6.5 or later

## Installation

Install the Python dependency from the repository root:

```bash
python -m pip install -r requirements.txt
```

## Running

```bash
python main.pyw
```

The app reads the `.env` next to `main.pyw`, wherever it is started from; set `SECOND_BRAIN_HUB_ENV` to use another file. Relative paths in it are relative to that file. Copy `.env.example` to start one. With nothing configured, the sidebar says which settings to add.

On Linux, `./packaging/install-desktop-entry.sh` adds a launcher with the app's icon. Remove any launcher you made by hand, so the window groups with this one.

## Keyboard shortcuts

| Shortcut | Action |
|---|---|
| `Ctrl+F` / `Ctrl+K` | Focus and select the search text |
| `Enter` / `Down` in search | Select the first visible topic |
| `F2` in the topic tree | Rename the selected topic |
| `F5` / `Ctrl+R` | Refresh the index and re-read the filenames in every topic folder |
| `Enter` in bookmarks | Open the selected bookmark |
| `Enter` in locations | Open the selected location |
| `Enter` in matching files | Open the selected file |

Click anywhere on a location, bookmark, or matching-file row to open it; pressing `Enter` opens it too. A row's checkbox and buttons keep their own actions. Tick bookmarks to move or delete several at once. Click a tag chip to filter the topic tree by exactly that tag; right-click it to rename or remove it.

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
    ├── indexer.py                    # Background scan, source topics, and export
    ├── json_lists.py                 # Tolerant parsing of the JSON topic lists
    ├── location_filter.py            # Location filter options and matching
    ├── models.py                     # Topic and bookmark models
    ├── naming.py                     # Portable topic-name validation
    ├── onenote.py                    # OneNote topic list (parsed by json_lists.py)
    ├── outlook.py                    # Outlook topic list (parsed by json_lists.py)
    ├── resources.py                  # Application resource lookup
    ├── scanner.py                    # Folder scanning
    ├── search.py                     # Search parsing and matching
    ├── subhub.py                     # Nested hub discovery
    ├── plm.py                        # PLM topic list (parsed by json_lists.py)
    ├── theme/                        # Palettes, fonts, icons, and stylesheet
    ├── ui/                           # Main window package
    │   ├── main_window.py            # MainWindow: composition, settings, topic details
    │   ├── refresh.py                # File watcher, background scan, stored data
    │   ├── filename_search.py        # Filename index and matching files
    │   ├── bookmark_actions.py       # Bookmark add, edit, move, copy, delete
    │   ├── location_actions.py       # Own links and folders, list entries
    │   ├── topic_moves.py            # Rename, merge, move, archive, undo
    │   ├── operations.py             # JSON and folder work behind them
    │   ├── creation.py               # New-topic planning and creation
    │   ├── orphans.py                # Data left under topics that no longer exist
    │   ├── widgets/                  # Header, sidebar tree, topic header, locations list,
    │   │                             # bookmarks, multi-selection panel, status bar
    │   └── dialogs/                  # Bookmark, tag, rename, confirm, new-topic dialogs
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

A topic that only appears in a topic list, whose entry names no PARA section and whose name has no prefix, is listed under Projects and marked the same way, with a note saying so.

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

The status bar then offers to undo the move. A prefix rename made with it is undone too.

### Rename

Rename validates names for Linux and Windows compatibility, then renames every eligible filesystem and Obsidian location. Case-only renames are supported.

If the new name already belongs to another topic, the dialog offers a merge. Cross-section merges can consolidate into one section or keep the existing sections. Existing destination items are never overwritten; conflicts are kept under `Merged from <old topic name>`.

After a rename, the old name is added as a tag so the topic remains searchable. The status bar offers an undo action for the latest rename or merge, restoring moved files and topic metadata where possible.

Topics in **Others** cannot be moved, archived, or renamed.

## Known limitations

- Automatic refreshes re-read filenames only for topics whose folders changed; files added inside an existing topic show up in filename search after F5.
- Only the latest rename, merge, move or archive can be undone.
- File conflicts are preserved for manual review; the app does not choose which version should replace the other.
- The Location filter compares paths as written. An added folder reached through a symlink or a differently spelled path does not count as inside a root.

## License

Second Brain Hub is licensed under the [GNU General Public License v3.0](LICENSE).
