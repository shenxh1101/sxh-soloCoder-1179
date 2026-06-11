"""Incremental updater: compares template render with existing project, backs up user changes.

Uses manifest file states (template_original / user_modified / user_created) to
annotate the diff preview and make safe update decisions.
"""

import datetime
import difflib
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from template_gen.manifest import (
    classify_project_files,
    get_files_by_state,
    load_manifest,
    update_manifest_after_update,
)

MANIFEST_FILENAME = ".template_gen_manifest.json"


def backup_project(project_dir: str) -> str:
    """Create a timestamped backup of the entire project directory."""
    project_path = Path(project_dir).resolve()
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = project_path.parent / f"{project_path.name}.backup_{timestamp}"
    shutil.copytree(project_path, backup_dir, ignore=_backup_ignore)
    return str(backup_dir)


def _backup_ignore(directory: str, files: List[str]) -> List[str]:
    ignore = {".git", "__pycache__", ".venv", "venv", "node_modules", ".pytest_cache"}
    return [f for f in files if f in ignore or f.endswith((".pyc", ".pyo"))]


# ── diff preview ────────────────────────────────────────────────────────────

_STATE_LABELS = {
    "template_original": "template",
    "user_modified": "modified",
    "user_created": "user",
}


def _get_state_map(project_dir: str) -> Dict[str, str]:
    """Build {rel_path: state_label} for diff display."""
    by_state = get_files_by_state(project_dir)
    mapping: Dict[str, str] = {}
    for state, paths in by_state.items():
        label = _STATE_LABELS.get(state, state)
        for p in paths:
            mapping[p] = label
    return mapping


def print_diff_preview(
    project_dir: str,
    new_render: Dict[str, str],
    template_name: str,
    template_version: str,
) -> None:
    """Print a detailed diff preview grouped by action: overwrite, add, keep, remove."""
    classification = classify_project_files(project_dir, new_render)
    state_map = _get_state_map(project_dir)

    unchanged = classification["unchanged"]
    changed = classification["changed"]
    template_new = classification["template_new"]
    template_removed = classification["template_removed"]
    user_only = classification["user_only"]

    total = len(unchanged) + len(changed) + len(template_new) + len(template_removed) + len(user_only)

    print(f"\n  {'='*50}")
    print(f"  Template: {template_name} v{template_version}")
    print(f"  Project:  {Path(project_dir).resolve()}")
    print(f"  Total tracked files: {total}")
    print(f"  {'='*50}")

    if changed:
        print(f"\n  \033[93m[OVERWRITE]  {len(changed)} file(s) — template changed, will be overwritten\033[0m")
        for f in changed:
            tag = state_map.get(f, "")
            extra = _describe_modification(project_dir, f, new_render.get(f, ""))
            tag_str = f"  \033[90m[{tag}]\033[0m" if tag else ""
            print(f"     M  {f}{tag_str}{extra}")

    if template_new:
        print(f"\n  \033[92m[ADD]       {len(template_new)} file(s) — new from template\033[0m")
        for f in template_new:
            print(f"     +  {f}")

    if template_removed:
        print(f"\n  \033[91m[REMOVE]    {len(template_removed)} file(s) — no longer in template\033[0m")
        for f in template_removed:
            tag = state_map.get(f, "")
            tag_str = f"  \033[90m[{tag}]\033[0m" if tag else ""
            print(f"     -  {f}{tag_str}")

    if unchanged:
        print(f"\n  \033[90m[KEEP]      {len(unchanged)} file(s) — unchanged\033[0m")
        if len(unchanged) <= 10:
            for f in unchanged:
                print(f"        {f}")
        else:
            print(f"        ({len(unchanged)} files, use --verbose for full list)")

    if user_only:
        print(f"\n  \033[96m[PROTECT]   {len(user_only)} file(s) — user-created, will not be touched\033[0m")
        for f in user_only:
            print(f"     ?  {f}")

    print(f"\n  \033[90m{'─'*50}\033[0m")

    has_impact = bool(changed or template_new or template_removed)
    if not has_impact:
        print("  \033[92mNo changes needed — project is up to date.\033[0m")

    print()


def _describe_modification(project_dir: str, rel_path: str, new_content: str) -> str:
    """Return a short description of what kind of modification was detected."""
    current_path = Path(project_dir) / rel_path
    if not current_path.exists():
        return ""
    try:
        old = current_path.read_text(encoding="utf-8")
    except Exception:
        return ""

    if old == new_content:
        return ""

    if len(old) == len(new_content):
        diff_lines = sum(1 for a, b in zip(old.splitlines(), new_content.splitlines()) if a != b)
        return f"  {diff_lines} line(s) differ"
    else:
        size_diff = len(new_content) - len(old)
        sign = "+" if size_diff > 0 else ""
        return f"  ({sign}{size_diff} bytes)"


# ── incremental update ──────────────────────────────────────────────────────

def incremental_update(
    project_dir: str,
    new_files: Dict[str, str],
    backup: bool = True,
    dry_run: bool = False,
    interactive: bool = True,
) -> Dict[str, Any]:
    """
    Incrementally update a project. Uses manifest to distinguish template
    files from user-created files. Backs up the project first, then overwrites
    only changed/new template files. User-only files are always protected.
    """
    project_path = Path(project_dir).resolve()

    if not project_path.exists():
        raise FileNotFoundError(f"Project directory not found: {project_dir}")

    classification = classify_project_files(project_dir, new_files)
    state_map = _get_state_map(project_dir)

    unchanged = classification["unchanged"]
    changed = classification["changed"]
    template_new = classification["template_new"]
    template_removed = classification["template_removed"]
    user_only = classification["user_only"]

    has_changes = bool(changed or template_new or template_removed or user_only)

    if not has_changes:
        return {
            "backup": None,
            "unchanged": unchanged,
            "changed": [],
            "added": [],
            "removed": [],
            "skipped": [],
            "user_only": user_only,
        }

    if backup and not dry_run:
        backup_dir = backup_project(project_dir)
    else:
        backup_dir = None

    skipped: List[str] = []
    applied_changed: List[str] = []
    applied_added: List[str] = []
    applied_removed: List[str] = []

    _print_update_header(
        changed, template_new, template_removed, user_only, state_map, project_dir, backup_dir
    )

    for rel_path in changed:
        apply = _ask_file_action(
            rel_path, "overwrite", project_path, new_files.get(rel_path), state_map, interactive
        )
        if apply:
            if not dry_run:
                target = project_path / rel_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(new_files[rel_path], encoding="utf-8")
            applied_changed.append(rel_path)
        else:
            skipped.append(rel_path)

    for rel_path in template_new:
        apply = _ask_file_action(
            rel_path, "add", project_path, new_files.get(rel_path), state_map, interactive
        )
        if apply:
            if not dry_run:
                target = project_path / rel_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(new_files[rel_path], encoding="utf-8")
            applied_added.append(rel_path)
        else:
            skipped.append(rel_path)

    for rel_path in template_removed:
        apply = _ask_file_action(
            rel_path, "remove", project_path, None, state_map, interactive
        )
        if apply:
            if not dry_run:
                target = project_path / rel_path
                if target.exists():
                    target.unlink()
            applied_removed.append(rel_path)
        else:
            skipped.append(rel_path)

    _print_update_result(applied_changed, applied_added, applied_removed, skipped, user_only)

    return {
        "backup": backup_dir,
        "unchanged": unchanged,
        "changed": applied_changed,
        "added": applied_added,
        "removed": applied_removed,
        "skipped": skipped,
        "user_only": user_only,
        "classifications": classification,
    }


def _print_update_header(
    changed: List[str],
    template_new: List[str],
    template_removed: List[str],
    user_only: List[str],
    state_map: Dict[str, str],
    project_dir: str,
    backup_dir: Optional[str],
) -> None:
    print(f"\n  \033[1mApplying changes to: {Path(project_dir).resolve()}\033[0m")
    if backup_dir:
        print(f"  \033[90mBackup: {backup_dir}\033[0m")
    if changed:
        print(f"  \033[93m{len(changed)} file(s) to overwrite\033[0m")
    if template_new:
        print(f"  \033[92m{len(template_new)} file(s) to add\033[0m")
    if template_removed:
        print(f"  \033[91m{len(template_removed)} file(s) to remove\033[0m")
    if user_only:
        print(f"  \033[96m{len(user_only)} user file(s) — always protected\033[0m")
    print()


def _ask_file_action(
    rel_path: str,
    action: str,
    project_path: Path,
    new_content: Optional[str],
    state_map: Dict[str, str],
    interactive: bool,
) -> bool:
    if not interactive:
        if action == "remove":
            return False
        return True

    tag = state_map.get(rel_path, "")
    tag_str = f" \033[90m[{tag}]\033[0m" if tag else ""

    if action == "overwrite":
        prompt = f"  Overwrite '{rel_path}'{tag_str}? [Y/n/s=show diff] "
    elif action == "add":
        prompt = f"  Add new file '{rel_path}'? [Y/n] "
    else:
        prompt = f"  Remove '{rel_path}'{tag_str}? [y/N] "

    ans = input(prompt).strip().lower()

    if ans == "s" and action == "overwrite" and new_content is not None:
        _show_file_diff(project_path, rel_path, new_content)
        ans = input(f"  Overwrite '{rel_path}'? [Y/n] ").strip().lower()

    if action == "remove":
        return ans in ("y", "yes")
    return ans not in ("n", "no")


def _show_file_diff(project_path: Path, rel_path: str, new_content: str) -> None:
    current_path = project_path / rel_path
    if current_path.exists():
        current_content = current_path.read_text(encoding="utf-8")
    else:
        current_content = "(new file)"

    diff = difflib.unified_diff(
        current_content.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile=f"a/{rel_path} (current)",
        tofile=f"b/{rel_path} (new template)",
    )
    print("  \033[90m" + "".join(diff) + "\033[0m")


def _print_update_result(
    applied_changed: List[str],
    applied_added: List[str],
    applied_removed: List[str],
    skipped: List[str],
    user_only: List[str],
) -> None:
    print(f"\n  \033[92mUpdate complete.\033[0m")
    if applied_changed:
        print(f"    {len(applied_changed)} file(s) updated")
    if applied_added:
        print(f"    {len(applied_added)} file(s) added")
    if applied_removed:
        print(f"    {len(applied_removed)} file(s) removed")
    if skipped:
        print(f"    {len(skipped)} file(s) skipped")
    if user_only:
        print(f"    {len(user_only)} user file(s) preserved")