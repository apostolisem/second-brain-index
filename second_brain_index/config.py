from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

MARKER_FILENAME = ".parahub.json"

# Points the app at a .env other than the one next to main.pyw.
ENV_PATH_VARIABLE = "SECOND_BRAIN_HUB_ENV"

DEFAULT_TOPIC_LIST_NAMES = {
    "onenote": "OneNote_topics_list.json",
    "outlook": "Outlook_topics_list.json",
    "plm": "PLM_topics_list.json",
}


class HubConfigError(RuntimeError):
    pass


@dataclass
class HubConfig:
    para_roots: list[Path]
    obsidian_root: Path | None
    obsidian_vault_name: str | None
    onenote_enabled: bool
    onenote_base_url: str | None
    onenote_topics_path: Path
    outlook_enabled: bool
    outlook_base_url: str | None
    outlook_topics_path: Path
    plm_enabled: bool
    plm_base_url: str | None
    plm_topics_path: Path
    marker_path: Path | None = None

    @classmethod
    def from_marker(cls, marker_path: Path) -> "HubConfig":
        try:
            # utf-8-sig: Windows editors like to prepend a byte order mark.
            data = json.loads(marker_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError) as exc:
            raise HubConfigError(f"Unable to read {marker_path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise HubConfigError(f"Invalid JSON in {marker_path}: {exc}") from exc
        if not isinstance(data, dict):
            raise HubConfigError(f"{marker_path} must contain a JSON object.")

        base_dir = marker_path.parent
        para_root_value = str(data.get("para_root", ".") or ".")
        para_roots = [normalize_path(para_root_value, base_dir)]

        obsidian = data.get("obsidian") or {}
        if not isinstance(obsidian, dict):
            raise HubConfigError(f"'obsidian' in {marker_path} must be an object.")
        obsidian_value = str(obsidian.get("vault", "") or "")
        obsidian_root = (
            normalize_path(obsidian_value, base_dir) if obsidian_value else None
        )
        obsidian_vault_name = str(obsidian.get("vault_name", "") or "") or None

        tool_settings = {}
        for tool in ("onenote", "outlook", "plm"):
            section = data.get(tool) or {}
            if not isinstance(section, dict):
                raise HubConfigError(f"'{tool}' in {marker_path} must be an object.")
            topics_value = str(
                section.get("topics_list", "") or DEFAULT_TOPIC_LIST_NAMES[tool]
            )
            tool_settings[tool] = (
                bool(section.get("enabled", False)),
                str(section.get("base_url", "") or "") or None,
                normalize_path(topics_value, base_dir),
            )

        return cls(
            para_roots=para_roots,
            obsidian_root=obsidian_root,
            obsidian_vault_name=obsidian_vault_name,
            onenote_enabled=tool_settings["onenote"][0],
            onenote_base_url=tool_settings["onenote"][1],
            onenote_topics_path=tool_settings["onenote"][2],
            outlook_enabled=tool_settings["outlook"][0],
            outlook_base_url=tool_settings["outlook"][1],
            outlook_topics_path=tool_settings["outlook"][2],
            plm_enabled=tool_settings["plm"][0],
            plm_base_url=tool_settings["plm"][1],
            plm_topics_path=tool_settings["plm"][2],
            marker_path=marker_path,
        )


@dataclass
class AppConfig:
    root_hub: HubConfig
    other_folders: list[Path]
    obsidian_reveal_active: bool
    obsidian_reveal_command: str
    db_path: Path
    auto_refresh: bool = True
    # Write Topics_export.json next to the lists and into each sub-hub.
    topics_export: bool = True
    # Where this came from, to point a first run at the file to fill in.
    env_path: Path | None = None

    @classmethod
    def load(cls, env_path: Path) -> "AppConfig":
        env = parse_env_file(env_path)
        base_dir = env_path.parent
        para_roots = parse_root_list(env.get("PARA_ROOTS", ""), base_dir)
        other_folders = parse_root_list(env.get("OTHER_FOLDERS", ""), base_dir)
        obsidian_root = parse_single_root(env.get("OBSIDIAN_VAULT", ""), base_dir)
        obsidian_vault_name = env.get("OBSIDIAN_VAULT_NAME", "") or None
        obsidian_reveal_active = parse_bool(env.get("OBSIDIAN_REVEAL_ACTIVE", ""))
        obsidian_reveal_command = env.get(
            "OBSIDIAN_REVEAL_COMMAND", "file-explorer:reveal-active-file"
        )
        def topics_list(tool: str) -> Path:
            # Same setting a sub-hub has as "topics_list" in its marker.
            value = env.get(f"{tool.upper()}_TOPICS_LIST", "") or DEFAULT_TOPIC_LIST_NAMES[tool]
            return normalize_path(value, base_dir)

        root_hub = HubConfig(
            para_roots=para_roots,
            obsidian_root=obsidian_root,
            obsidian_vault_name=obsidian_vault_name,
            onenote_enabled=parse_bool(env.get("ONENOTE_ENABLED", "")),
            onenote_base_url=env.get("ONENOTE_BASE_URL", "") or None,
            onenote_topics_path=topics_list("onenote"),
            outlook_enabled=parse_bool(env.get("OUTLOOK_ENABLED", "")),
            outlook_base_url=env.get("OUTLOOK_BASE_URL", "") or None,
            outlook_topics_path=topics_list("outlook"),
            plm_enabled=parse_bool(env.get("PLM_ENABLED", "")),
            plm_base_url=env.get("PLM_BASE_URL", "") or None,
            plm_topics_path=topics_list("plm"),
        )

        db_value = env.get("DB_PATH", "second_brain_index.db")
        db_path = normalize_path(db_value, base_dir)
        auto_refresh = parse_bool(env.get("AUTO_REFRESH", "true"))
        return cls(
            root_hub=root_hub,
            other_folders=other_folders,
            obsidian_reveal_active=obsidian_reveal_active,
            obsidian_reveal_command=obsidian_reveal_command,
            db_path=db_path,
            auto_refresh=auto_refresh,
            topics_export=parse_bool(env.get("TOPICS_EXPORT", "true")),
            env_path=env_path.resolve(),
        )


def settings_group(env_path: Path | None) -> str:
    """The QSettings group holding one configuration's window and session.

    Two checkouts, each with its own .env, list different topics; sharing the
    last topic or the expanded sections between them makes no sense. Empty
    for a configuration that did not come from a file.
    """
    if env_path is None:
        return ""
    digest = hashlib.sha1(str(env_path).encode("utf-8")).hexdigest()[:12]
    return f"profiles/{digest}"


def default_env_path(app_dir: Path) -> Path:
    """The .env to load: ``SECOND_BRAIN_HUB_ENV`` if set, else the app's own.

    Not the working directory: started from anywhere else, the app would
    otherwise find no configuration and create an empty database there.
    """
    override = os.environ.get(ENV_PATH_VARIABLE, "").strip()
    if override:
        return Path(os.path.expanduser(override)).resolve()
    return app_dir.resolve() / ".env"


def parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    try:
        # A byte order mark would otherwise become part of the first key.
        content = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        # Written in the machine's legacy code page rather than UTF-8.
        content = path.read_text()

    env: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = parse_env_value(value)
    return env


def parse_env_value(raw: str) -> str:
    """A value with its quotes or a trailing ``# comment`` taken off.

    A quoted value keeps everything inside the quotes, ``#`` included; an
    unquoted one ends at a ``#`` that follows whitespace, so a ``#`` inside a
    URL or path survives.
    """
    value = raw.strip()
    if value[:1] in {'"', "'"}:
        end = value.find(value[0], 1)
        if end != -1:
            return value[1:end]
        return value.strip("\"'")
    match = re.search(r"\s#", value)
    if match:
        value = value[: match.start()]
    return value.strip()


def parse_root_list(value: str, base_dir: Path) -> list[Path]:
    if not value:
        return []
    parts = [part.strip() for part in value.split(";") if part.strip()]
    return [normalize_path(part, base_dir) for part in parts]


def parse_single_root(value: str, base_dir: Path) -> Path | None:
    if not value:
        return None
    return normalize_path(value, base_dir)


def normalize_path(raw: str, base_dir: Path) -> Path:
    expanded = os.path.expanduser(raw.strip())
    path = Path(expanded)
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def parse_bool(value: str) -> bool:
    lowered = value.strip().lower()
    return lowered in {"1", "true", "yes", "y", "on"}
