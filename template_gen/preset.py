"""Preset save/load system for reusing project generation choices."""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional


def get_preset_dir() -> Path:
    home = Path.home()
    preset_dir = home / ".template_gen" / "presets"
    preset_dir.mkdir(parents=True, exist_ok=True)
    return preset_dir


def save_preset(name: str, template_name: str, variables: Dict[str, Any]) -> str:
    """Save a preset to disk. Returns the preset file path."""
    preset_dir = get_preset_dir()
    safe_name = _safe_filename(name)
    filepath = preset_dir / f"{safe_name}.json"

    data = {
        "name": name,
        "template_name": template_name,
        "variables": variables,
    }
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    return str(filepath)


def load_preset(name: str) -> Optional[Dict[str, Any]]:
    """Load a preset by name. Returns None if not found."""
    preset_dir = get_preset_dir()
    safe_name = _safe_filename(name)
    filepath = preset_dir / f"{safe_name}.json"

    if not filepath.exists():
        return None

    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def list_presets() -> List[Dict[str, str]]:
    """List all saved presets."""
    preset_dir = get_preset_dir()
    presets = []

    for entry in preset_dir.glob("*.json"):
        try:
            with open(entry, "r", encoding="utf-8") as f:
                data = json.load(f)
            presets.append({
                "name": data.get("name", entry.stem),
                "filename": entry.name,
                "template_name": data.get("template_name", ""),
                "path": str(entry),
            })
        except (json.JSONDecodeError, KeyError):
            continue

    return sorted(presets, key=lambda p: p["name"])


def delete_preset(name: str) -> bool:
    """Delete a preset by name. Returns True if deleted."""
    preset_dir = get_preset_dir()
    safe_name = _safe_filename(name)
    filepath = preset_dir / f"{safe_name}.json"

    if filepath.exists():
        filepath.unlink()
        return True
    return False


def _safe_filename(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)