"""Preset save/load system for reusing project generation choices."""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional


def get_preset_dir() -> Path:
    home = Path.home()
    preset_dir = home / ".template_gen" / "presets"
    preset_dir.mkdir(parents=True, exist_ok=True)
    return preset_dir


def save_preset(
    name: str,
    template_name: str,
    template_version: str,
    variables: Dict[str, Any],
) -> str:
    """Save a preset to disk. Returns the preset file path."""
    preset_dir = get_preset_dir()
    safe_name = _safe_filename(name)
    filepath = preset_dir / f"{safe_name}.json"

    data = {
        "name": name,
        "template_name": template_name,
        "template_version": template_version,
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


def check_preset_match(
    preset_name: str,
    current_template_name: str,
) -> Optional[str]:
    """
    Check if a preset matches the current template. Returns None if ok,
    or an error message string if there is a mismatch.
    """
    data = load_preset(preset_name)
    if data is None:
        return f"Preset '{preset_name}' not found."

    preset_template = data.get("template_name", "")
    if preset_template.lower() != current_template_name.lower():
        return (
            f"Preset '{preset_name}' was created for template '{preset_template}', "
            f"but you selected '{current_template_name}'. Variables may not be compatible."
        )
    return None


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
                "template_version": data.get("template_version", ""),
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