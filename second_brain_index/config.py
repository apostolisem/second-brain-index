from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

MARKER_FILENAME = ".parahub.json"

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
            data = json.loads(marker_path.read_text(encoding="utf-8"))
        except OSError as exc:
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
    obsidian_reveal_delay_ms: int
    db_path: Path
    auto_refresh: bool = True

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
        obsidian_reveal_delay_ms = parse_int(
            env.get("OBSIDIAN_REVEAL_DELAY_MS", "500"), default=500
        )
        root_hub = HubConfig(
            para_roots=para_roots,
            obsidian_root=obsidian_root,
            obsidian_vault_name=obsidian_vault_name,
            onenote_enabled=parse_bool(env.get("ONENOTE_ENABLED", "")),
            onenote_base_url=env.get("ONENOTE_BASE_URL", "") or None,
            onenote_topics_path=normalize_path(
                DEFAULT_TOPIC_LIST_NAMES["onenote"], base_dir
            ),
            outlook_enabled=parse_bool(env.get("OUTLOOK_ENABLED", "")),
            outlook_base_url=env.get("OUTLOOK_BASE_URL", "") or None,
            outlook_topics_path=normalize_path(
                DEFAULT_TOPIC_LIST_NAMES["outlook"], base_dir
            ),
            plm_enabled=parse_bool(env.get("PLM_ENABLED", "")),
            plm_base_url=env.get("PLM_BASE_URL", "") or None,
            plm_topics_path=normalize_path(
                DEFAULT_TOPIC_LIST_NAMES["plm"], base_dir
            ),
        )

        db_value = env.get("DB_PATH", "second_brain_index.db")
        db_path = normalize_path(db_value, base_dir)
        auto_refresh = parse_bool(env.get("AUTO_REFRESH", "true"))
        return cls(
            root_hub=root_hub,
            other_folders=other_folders,
            obsidian_reveal_active=obsidian_reveal_active,
            obsidian_reveal_command=obsidian_reveal_command,
            obsidian_reveal_delay_ms=obsidian_reveal_delay_ms,
            db_path=db_path,
            auto_refresh=auto_refresh,
        )


def parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    env: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"").strip("'")
        env[key] = value
    return env


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


def parse_int(value: str, default: int) -> int:
    try:
        return int(value.strip())
    except ValueError:
        return default
