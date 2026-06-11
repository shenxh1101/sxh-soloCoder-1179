"""Project auditor: scans a generated project against its manifest and reports file status."""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from template_gen.manifest import load_manifest, MANIFEST_FILENAME


def audit_project(project_dir: str) -> Dict[str, Any]:
    """
    Scan a project directory against its manifest.
    Returns a dict with categorized file lists and counts.
    """
    project_path = Path(project_dir).resolve()
    manifest = load_manifest(str(project_path))

    if not manifest:
        return {
            "has_manifest": False,
            "template_original": [],
            "user_modified": [],
            "user_created": [],
            "missing_from_disk": [],
            "orphaned_on_disk": [],
            "name_conflicts": [],
        }

    manifest_files: Dict[str, Dict[str, str]] = manifest.get("files", {})
    if manifest_files and isinstance(next(iter(manifest_files.values())), str):
        manifest_files = {p: {"hash": h, "state": "template_original"} for p, h in manifest_files.items()}

    template_original: List[Dict[str, str]] = []
    user_modified: List[Dict[str, str]] = []
    user_created: List[Dict[str, str]] = []
    missing_from_disk: List[Dict[str, str]] = []
    orphaned_on_disk: List[Dict[str, str]] = []

    disk_files: Set[str] = set()
    for root, dirs, files in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".venv", "venv", "node_modules")]
        for f in files:
            if f.endswith((".pyc", ".pyo")) or f == MANIFEST_FILENAME:
                continue
            rel = os.path.relpath(os.path.join(root, f), project_path)
            rel = rel.replace("\\", "/")
            disk_files.add(rel)

    for rel_path, entry in sorted(manifest_files.items()):
        state = entry.get("state", "template_original")
        if isinstance(entry, str):
            state = "template_original"
            manifest_hash = entry
        else:
            manifest_hash = entry.get("hash", "")

        if rel_path not in disk_files:
            missing_from_disk.append({"path": rel_path, "state": state, "manifest_hash": manifest_hash})
            continue

        target = project_path / rel_path
        try:
            current = target.read_text(encoding="utf-8")
        except Exception:
            current = ""
        import hashlib
        current_hash = hashlib.sha256(current.encode()).hexdigest()

        entry_data = {"path": rel_path, "state": state, "manifest_hash": manifest_hash, "current_hash": current_hash}

        if state == "template_original" and current_hash == manifest_hash:
            template_original.append(entry_data)
        elif state == "template_original" and current_hash != manifest_hash:
            user_modified.append(entry_data)
        elif state == "user_modified":
            entry_data["state"] = "user_modified"
            user_modified.append(entry_data)
        elif state == "user_created":
            entry_data["state"] = "user_created"
            user_created.append(entry_data)

    for rel_path in sorted(disk_files - set(manifest_files.keys())):
        target = project_path / rel_path
        try:
            current = target.read_text(encoding="utf-8")
        except Exception:
            current = ""
        import hashlib
        current_hash = hashlib.sha256(current.encode()).hexdigest()
        orphaned_on_disk.append({"path": rel_path, "state": "orphaned", "current_hash": current_hash})

    return {
        "has_manifest": True,
        "template_name": manifest.get("template_name", ""),
        "template_version": manifest.get("template_version", ""),
        "generated_at": manifest.get("generated_at", ""),
        "template_original": template_original,
        "user_modified": user_modified,
        "user_created": user_created,
        "missing_from_disk": missing_from_disk,
        "orphaned_on_disk": orphaned_on_disk,
    }


def repair_manifest(project_dir: str) -> Dict[str, Any]:
    """
    Repair the manifest by removing missing entries and adding orphaned files.
    Returns a dict with repair summary.
    """
    audit = audit_project(project_dir)
    if not audit["has_manifest"]:
        return {"repaired": False, "reason": "No manifest found"}

    manifest = load_manifest(project_dir)
    manifest_files: Dict[str, Any] = manifest.get("files", {})
    if manifest_files and isinstance(next(iter(manifest_files.values())), str):
        manifest_files = {p: {"hash": h, "state": "template_original"} for p, h in manifest_files.items()}

    removed_count = 0
    for entry in audit["missing_from_disk"]:
        path = entry["path"]
        if path in manifest_files:
            del manifest_files[path]
            removed_count += 1

    added_count = 0
    for entry in audit["orphaned_on_disk"]:
        path = entry["path"]
        manifest_files[path] = {"hash": entry["current_hash"], "state": "user_created"}
        added_count += 1

    for entry in audit["template_original"]:
        if entry["path"] in manifest_files:
            manifest_files[entry["path"]] = {"hash": entry["manifest_hash"], "state": "template_original"}

    for entry in audit["user_modified"]:
        if entry["path"] in manifest_files:
            manifest_files[entry["path"]] = {"hash": entry["current_hash"], "state": "user_modified"}

    for entry in audit["user_created"]:
        if entry["path"] in manifest_files:
            manifest_files[entry["path"]] = {"hash": entry["current_hash"], "state": "user_created"}

    manifest["files"] = manifest_files
    import json
    manifest_path = Path(project_dir) / MANIFEST_FILENAME
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    return {
        "repaired": True,
        "removed_missing": removed_count,
        "added_orphaned": added_count,
    }


def print_audit_report(audit: Dict[str, Any]) -> None:
    """Print a formatted audit report."""
    if not audit["has_manifest"]:
        print("\n  \033[93mNo manifest found — this directory was not generated by template-gen.\033[0m\n")
        return

    t_orig = audit["template_original"]
    t_mod = audit["user_modified"]
    t_created = audit["user_created"]
    t_missing = audit["missing_from_disk"]
    t_orphan = audit["orphaned_on_disk"]

    total = len(t_orig) + len(t_mod) + len(t_created) + len(t_missing) + len(t_orphan)

    print(f"\n  {'='*50}")
    print(f"  Project Audit")
    print(f"  Template:  {audit.get('template_name', 'N/A')} v{audit.get('template_version', '?')}")
    print(f"  Generated: {audit.get('generated_at', 'N/A')}")
    print(f"  Total tracked: {total}")
    print(f"  {'='*50}")

    _print_audit_section("Template Original", t_orig, "\033[92m", "✓")
    _print_audit_section("User Modified", t_mod, "\033[93m", "~")
    _print_audit_section("User Created", t_created, "\033[96m", "+")
    _print_audit_section("Missing from Disk", t_missing, "\033[91m", "✗")
    _print_audit_section("Orphaned on Disk", t_orphan, "\033[90m", "?")

    has_problems = bool(t_missing or t_orphan)
    if has_problems:
        print(f"\n  \033[93m⚠  Manifest needs repair — {len(t_missing)} missing + {len(t_orphan)} orphaned.\033[0m")
        print(f"  Run 'template-gen audit <dir> --repair' to fix.\n")
    else:
        print(f"\n  \033[92m✓ Manifest is consistent with filesystem.\033[0m\n")


def _print_audit_section(title: str, items: List[Dict[str, str]], color: str, icon: str) -> None:
    if not items:
        return
    print(f"\n  {color}[{title}]  {len(items)} file(s)\033[0m")
    for item in items:
        path = item.get("path", "?")
        extra = ""
        if "manifest_hash" in item and "current_hash" in item and item["manifest_hash"] != item["current_hash"]:
            extra = "  \033[90m(hash differs)\033[0m"
        print(f"    {color}{icon}  {path}{extra}\033[0m")