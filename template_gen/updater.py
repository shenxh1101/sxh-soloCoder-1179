"""Incremental updater: compares template render with existing project, backs up user changes."""

import datetime
import filecmp
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


def backup_project(project_dir: str) -> str:
    """Create a timestamped backup of the entire project directory."""
    project_path = Path(project_dir).resolve()
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = project_path.parent / f"{project_path.name}.backup_{timestamp}"

    shutil.copytree(project_path, backup_dir)
    return str(backup_dir)


def compute_diff(
    project_dir: str,
    new_files: Dict[str, str],
) -> Tuple[List[str], List[str], List[str], List[str]]:
    """
    Compare existing project files with newly rendered template output.
    Returns (unchanged, changed, added, removed) file lists (relative paths).
    """
    project_path = Path(project_dir).resolve()
    unchanged: List[str] = []
    changed: List[str] = []
    added: List[str] = []
    removed: List[str] = []

    existing_files = set()
    for root, dirs, files in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".venv", "venv", "node_modules")]
        for f in files:
            if f.endswith((".pyc", ".pyo")):
                continue
            rel = os.path.relpath(os.path.join(root, f), project_path)
            rel = rel.replace("\\", "/")
            existing_files.add(rel)

    new_file_paths = set(new_files.keys())

    for rel_path in existing_files & new_file_paths:
        current_path = project_path / rel_path
        current_content = current_path.read_text(encoding="utf-8") if current_path.exists() else ""
        new_content = new_files[rel_path]

        if current_content == new_content:
            unchanged.append(rel_path)
        else:
            changed.append(rel_path)

    for rel_path in new_file_paths - existing_files:
        added.append(rel_path)

    removed = list(existing_files - new_file_paths)
    return unchanged, changed, added, removed


def incremental_update(
    project_dir: str,
    new_files: Dict[str, str],
    backup: bool = True,
    dry_run: bool = False,
    interactive: bool = True,
) -> Dict[str, Any]:
    """
    Incrementally update a project. Backs up the project first, then overwrites
    only changed/new template files. If interactive, prompts for each conflicted file.
    """
    project_path = Path(project_dir).resolve()

    if not project_path.exists():
        raise FileNotFoundError(f"Project directory not found: {project_dir}")

    unchanged, changed, added, removed = compute_diff(project_dir, new_files)

    if not changed and not added and not removed:
        return {
            "backup": None,
            "unchanged": unchanged,
            "changed": [],
            "added": [],
            "removed": [],
            "skipped": [],
        }

    if backup and not dry_run:
        backup_dir = backup_project(project_dir)
    else:
        backup_dir = None

    skipped: List[str] = []
    applied_changed: List[str] = []
    applied_added: List[str] = []

    print(f"\n  Template changes detected:")
    if changed:
        print(f"    \033[93m{len(changed)} file(s) modified\033[0m")
    if added:
        print(f"    \033[92m{len(added)} file(s) added\033[0m")
    if removed:
        print(f"    \033[91m{len(removed)} file(s) not in template\033[0m")
    if backup_dir:
        print(f"  \033[90mBackup created at: {backup_dir}\033[0m")

    if interactive and changed:
        print(f"\n  Files with conflicts ({len(changed)}):")
        for rel_path in changed:
            print(f"    - {rel_path}")

        answer = input(f"\n  Apply changes to {len(changed)} file(s)? [Y/n] ").strip().lower()
        if answer in ("n", "no"):
            print("  \033[93mSkipped all changed files.\033[0m")
            return {
                "backup": backup_dir,
                "unchanged": unchanged,
                "changed": [],
                "added": applied_added,
                "removed": [],
                "skipped": changed,
            }

    for rel_path in changed:
        if interactive:
            ans = input(f"  Overwrite '{rel_path}'? [Y/n/s=show diff] ").strip().lower()
            if ans == "s":
                _show_diff(project_path, rel_path, new_files[rel_path])
                ans = input(f"  Overwrite '{rel_path}'? [Y/n] ").strip().lower()
            if ans in ("n", "no"):
                skipped.append(rel_path)
                continue

        if not dry_run:
            target = project_path / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(new_files[rel_path], encoding="utf-8")
        applied_changed.append(rel_path)

    for rel_path in added:
        if interactive:
            ans = input(f"  Add new file '{rel_path}'? [Y/n] ").strip().lower()
            if ans in ("n", "no"):
                skipped.append(rel_path)
                continue

        if not dry_run:
            target = project_path / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(new_files[rel_path], encoding="utf-8")
        applied_added.append(rel_path)

    for rel_path in removed:
        if interactive:
            ans = input(f"  Remove '{rel_path}' (no longer in template)? [y/N] ").strip().lower()
            if ans not in ("y", "yes"):
                skipped.append(rel_path)
                continue

        if not dry_run:
            target = project_path / rel_path
            if target.exists():
                target.unlink()

    print(f"\n  \033[92mUpdate complete.\033[0m")
    print(f"    {len(applied_changed)} file(s) updated")
    print(f"    {len(applied_added)} file(s) added")
    if skipped:
        print(f"    {len(skipped)} file(s) skipped")

    return {
        "backup": backup_dir,
        "unchanged": unchanged,
        "changed": applied_changed,
        "added": applied_added,
        "removed": applied_added,
        "skipped": skipped,
    }


def _show_diff(project_path: Path, rel_path: str, new_content: str) -> None:
    current_path = project_path / rel_path
    if current_path.exists():
        current_content = current_path.read_text(encoding="utf-8")
    else:
        current_content = "(new file)"

    import difflib
    diff = difflib.unified_diff(
        current_content.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile=f"a/{rel_path} (current)",
        tofile=f"b/{rel_path} (new template)",
    )
    print("  \033[90m" + "".join(diff) + "\033[0m")