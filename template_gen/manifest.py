"""Generation manifest: records what was generated with file state tracking.

File states:
  - template_original : unchanged from template render
  - user_modified     : originated from template but user has changed it
  - user_created      : not from template at all, user created it

This allows `update` to correctly distinguish file origins across multiple updates.
"""

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

MANIFEST_FILENAME = ".template_gen_manifest.json"

STATES = ("template_original", "user_modified", "user_created")


def _hash_content(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _file_entry(hash_val: str, state: str) -> Dict[str, str]:
    return {"hash": hash_val, "state": state}


# ── write / load ────────────────────────────────────────────────────────────

def write_manifest(
    project_dir: str,
    template_name: str,
    template_version: str,
    variables: Dict[str, Any],
    rendered_files: Dict[str, str],
) -> str:
    project_path = Path(project_dir).resolve()
    project_path.mkdir(parents=True, exist_ok=True)

    files_entry: Dict[str, Dict[str, str]] = {}
    for rel_path, content in sorted(rendered_files.items()):
        files_entry[rel_path] = _file_entry(_hash_content(content), "template_original")

    manifest = {
        "generated_at": datetime.now().isoformat(),
        "template_name": template_name,
        "template_version": template_version,
        "variables": variables,
        "files": files_entry,
    }

    manifest_path = project_path / MANIFEST_FILENAME
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    return str(manifest_path)


def load_manifest(project_dir: str) -> Optional[Dict[str, Any]]:
    manifest_path = Path(project_dir) / MANIFEST_FILENAME
    if not manifest_path.exists():
        return None
    with open(manifest_path, "r", encoding="utf-8") as f:
        return json.load(f)


# ── file state helpers ──────────────────────────────────────────────────────

def _get_state_from_manifest(project_dir: str, rel_path: str) -> Optional[str]:
    m = load_manifest(project_dir)
    if not m:
        return None
    entry = m.get("files", {}).get(rel_path)
    if entry is None:
        return None
    if isinstance(entry, dict):
        return entry.get("state")
    return None


def get_files_by_state(project_dir: str) -> Dict[str, List[str]]:
    """Return {state: [paths]} for all tracked files."""
    m = load_manifest(project_dir)
    result: Dict[str, List[str]] = {"template_original": [], "user_modified": [], "user_created": []}
    if not m:
        return result
    for path, entry in m.get("files", {}).items():
        if isinstance(entry, dict):
            state = entry.get("state", "template_original")
        else:
            state = "template_original"
        result.setdefault(state, []).append(path)
    return result


def get_template_owned_files(project_dir: str) -> Set[str]:
    """Return paths that originated from template (original + modified)."""
    by_state = get_files_by_state(project_dir)
    return set(by_state.get("template_original", []) + by_state.get("user_modified", []))


# ── classification ──────────────────────────────────────────────────────────

def classify_project_files(
    project_dir: str,
    new_render: Dict[str, str],
) -> Dict[str, Any]:
    """
    Classify files into five categories for safe incremental update.
    Returns dict with keys: unchanged, changed, template_new, template_removed, user_only.

    Uses manifest file states to distinguish template-origin from user-created.
    A file that was user_modified in manifest but matches new template render is
    a candidate for overwrite (it will be in `changed`).
    """
    project_path = Path(project_dir).resolve()
    manifest = load_manifest(project_dir)

    template_owned = get_template_owned_files(project_dir) if manifest else set()

    existing_files: Set[str] = set()
    for root, dirs, files in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".venv", "venv", "node_modules")]
        for f in files:
            if f.endswith((".pyc", ".pyo")) or f == MANIFEST_FILENAME:
                continue
            rel = os.path.relpath(os.path.join(root, f), project_path)
            rel = rel.replace("\\", "/")
            existing_files.add(rel)

    new_file_paths = set(new_render.keys())

    unchanged: List[str] = []
    changed: List[str] = []
    template_new: List[str] = []
    template_removed: List[str] = []
    user_only: List[str] = []

    both = existing_files & new_file_paths
    for rel_path in both:
        current_path = project_path / rel_path
        if current_path.exists():
            current_content = current_path.read_text(encoding="utf-8")
        else:
            current_content = ""
        new_content = new_render[rel_path]

        if current_content == new_content:
            unchanged.append(rel_path)
        else:
            changed.append(rel_path)

    for rel_path in new_file_paths - existing_files:
        template_new.append(rel_path)

    for rel_path in existing_files - new_file_paths:
        if template_owned and rel_path in template_owned:
            template_removed.append(rel_path)
        else:
            user_only.append(rel_path)

    return {
        "unchanged": sorted(unchanged),
        "changed": sorted(changed),
        "template_new": sorted(template_new),
        "template_removed": sorted(template_removed),
        "user_only": sorted(user_only),
    }


# ── update manifest after apply ─────────────────────────────────────────────

def update_manifest_after_update(
    project_dir: str,
    template_name: str,
    template_version: str,
    variables: Dict[str, Any],
    new_render: Dict[str, str],
    classifications: Dict[str, Any],
    resolved_conflicts: Optional[List[Dict[str, str]]] = None,
) -> str:
    """
    Update the manifest after an incremental update.
    - New template files → template_original
    - Overwritten files that were user_modified → stays user_modified
    - Skipped files → keep existing state, mark as user_modified if template changed
    - User-only files → keep user_created
    - Conflict: keep_user → stays user_created
    - Conflict: overwrite_user → becomes template_original
    - Conflict: save_as_alternate → alternate path added as template_original
    - Conflict: preview_only → keep existing entry unchanged
    """

    project_path = Path(project_dir).resolve()
    manifest = load_manifest(project_dir)
    resolved_conflicts = resolved_conflicts or []

    old_entries: Dict[str, Dict[str, str]] = {}
    if manifest:
        for path, entry in manifest.get("files", {}).items():
            if isinstance(entry, dict):
                old_entries[path] = dict(entry)
            else:
                old_entries[path] = {"hash": str(entry), "state": "template_original"}

    skipped_set = set(classifications.get("skipped", []))
    unchanged_set = set(classifications.get("unchanged", []))
    changed_set = set(classifications.get("changed", []))
    added_set = set(classifications.get("template_new", []))
    user_only_set = set(classifications.get("user_only", []))

    new_manifest_files: Dict[str, Dict[str, str]] = {}

    for rel_path, content in sorted(new_render.items()):
        template_hash = _hash_content(content)

        if rel_path in unchanged_set:
            new_manifest_files[rel_path] = _file_entry(template_hash, "template_original")
        elif rel_path in changed_set:
            prev_entry = old_entries.get(rel_path)
            prev_state = prev_entry.get("state") if prev_entry else "template_original"
            if prev_state in ("user_modified", "user_created"):
                new_manifest_files[rel_path] = _file_entry(template_hash, "user_modified")
            else:
                new_manifest_files[rel_path] = _file_entry(template_hash, "template_original")
        elif rel_path in added_set:
            new_manifest_files[rel_path] = _file_entry(template_hash, "template_original")
        elif rel_path in skipped_set:
            prev_entry = old_entries.get(rel_path)
            if prev_entry:
                if prev_entry.get("state") == "template_original" and prev_entry.get("hash") != template_hash:
                    new_manifest_files[rel_path] = _file_entry(prev_entry.get("hash", ""), "user_modified")
                else:
                    new_manifest_files[rel_path] = dict(prev_entry)
            else:
                new_manifest_files[rel_path] = _file_entry(template_hash, "template_original")

    for conflict in resolved_conflicts:
        if not conflict:
            continue
        path = conflict.get("path", "")
        action = conflict.get("action", "")
        template_content = new_render.get(path, "")
        template_hash = _hash_content(template_content) if template_content else ""

        if action == "keep_user":
            prev_entry = old_entries.get(path)
            if prev_entry:
                new_manifest_files[path] = dict(prev_entry)
            else:
                existing = project_path / path
                if existing.exists():
                    new_manifest_files[path] = _file_entry(
                        _hash_content(existing.read_text(encoding="utf-8")), "user_created"
                    )
        elif action == "preview_only":
            prev_entry = old_entries.get(path)
            if prev_entry:
                new_manifest_files[path] = dict(prev_entry)
            else:
                existing = project_path / path
                if existing.exists():
                    new_manifest_files[path] = _file_entry(
                        _hash_content(existing.read_text(encoding="utf-8")), "user_created"
                    )
        elif action == "overwrite_user":
            new_manifest_files[path] = _file_entry(template_hash, "template_original")
        elif action == "save_as_alternate":
            alt = conflict.get("alternate_path", "")
            if alt:
                new_manifest_files[alt] = _file_entry(template_hash, "template_original")
            prev_entry = old_entries.get(path)
            if prev_entry:
                new_manifest_files[path] = dict(prev_entry)
            else:
                existing = project_path / path
                if existing.exists():
                    new_manifest_files[path] = _file_entry(
                        _hash_content(existing.read_text(encoding="utf-8")), "user_created"
                    )

    for rel_path in user_only_set:
        if rel_path not in new_manifest_files:
            target = project_path / rel_path
            if target.exists():
                new_manifest_files[rel_path] = _file_entry(
                    _hash_content(target.read_text(encoding="utf-8")),
                    "user_created",
                )

    manifest_data = {
        "generated_at": datetime.now().isoformat(),
        "template_name": template_name,
        "template_version": template_version,
        "variables": variables,
        "files": new_manifest_files,
    }

    manifest_path = project_path / MANIFEST_FILENAME
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest_data, f, indent=2, ensure_ascii=False)

    return str(manifest_path)