"""Project auditor: scans a generated project against its manifest and reports file status.

Also supports workspace-level batch audit and repair dry-run preview.
"""

import json
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


def preview_repair(project_dir: str) -> Dict[str, Any]:
    """
    Dry-run preview of what repair_manifest would do.
    Returns a dict with planned changes without modifying the manifest.
    """
    audit = audit_project(project_dir)
    if not audit["has_manifest"]:
        return {"preview": True, "reason": "No manifest found", "would_remove": [], "would_add": []}

    would_remove = []
    for entry in audit["missing_from_disk"]:
        would_remove.append({
            "path": entry["path"],
            "state": entry["state"],
            "reason": "file missing from disk",
        })

    would_add = []
    for entry in audit["orphaned_on_disk"]:
        would_add.append({
            "path": entry["path"],
            "state": "user_created",
            "reason": "file on disk not in manifest",
        })

    return {
        "preview": True,
        "would_remove": would_remove,
        "would_add": would_add,
        "remove_count": len(would_remove),
        "add_count": len(would_add),
    }


def print_repair_preview(preview: Dict[str, Any]) -> None:
    """Print a formatted repair dry-run preview."""
    if "reason" in preview:
        print(f"\n  \033[93m{preview['reason']}\033[0m\n")
        return

    would_remove = preview.get("would_remove", [])
    would_add = preview.get("would_add", [])

    print(f"\n  \033[1m{'='*50}\033[0m")
    print(f"  \033[1m  Repair Dry-Run Preview\033[0m")
    print(f"  \033[1m{'='*50}\033[0m")

    if not would_remove and not would_add:
        print(f"  \033[92m  Nothing to repair — manifest is consistent.\033[0m\n")
        return

    if would_remove:
        print(f"\n  \033[91m  Would REMOVE {len(would_remove)} entry(s) from manifest:\033[0m")
        for entry in would_remove:
            print(f"    \033[91m  ✗\033[0m {entry['path']}  \033[90m({entry['reason']})\033[0m")

    if would_add:
        print(f"\n  \033[92m  Would ADD {len(would_add)} entry(s) to manifest as user_created:\033[0m")
        for entry in would_add:
            print(f"    \033[92m  +\033[0m {entry['path']}  \033[90m({entry['reason']})\033[0m")

    print(f"\n  \033[93m  ⚠  These changes have NOT been applied yet.\033[0m")
    print(f"  \033[93m  Run with --repair --confirm to apply.\033[0m\n")


# ── workspace batch audit ───────────────────────────────────────────────────

def audit_workspace(workspace_dir: str) -> Dict[str, Any]:
    """
    Scan a workspace directory for all projects that have a manifest.
    Returns aggregated results grouped by template name, version, and file state.
    """
    workspace_path = Path(workspace_dir).resolve()
    if not workspace_path.exists():
        return {"workspace": str(workspace_path), "error": "Directory not found", "projects": []}

    projects: List[Dict[str, Any]] = []
    manifest_paths = list(workspace_path.rglob(MANIFEST_FILENAME))

    for manifest_path in manifest_paths:
        project_dir = str(manifest_path.parent)
        try:
            audit_result = audit_project(project_dir)
        except Exception:
            continue

        if not audit_result.get("has_manifest"):
            continue

        projects.append({
            "project_dir": project_dir,
            "template_name": audit_result.get("template_name", "?"),
            "template_version": audit_result.get("template_version", "?"),
            "generated_at": audit_result.get("generated_at", ""),
            "template_original": len(audit_result.get("template_original", [])),
            "user_modified": len(audit_result.get("user_modified", [])),
            "user_created": len(audit_result.get("user_created", [])),
            "missing_from_disk": len(audit_result.get("missing_from_disk", [])),
            "orphaned_on_disk": len(audit_result.get("orphaned_on_disk", [])),
            "total_tracked": (
                len(audit_result.get("template_original", []))
                + len(audit_result.get("user_modified", []))
                + len(audit_result.get("user_created", []))
                + len(audit_result.get("missing_from_disk", []))
                + len(audit_result.get("orphaned_on_disk", []))
            ),
            "needs_sync": bool(
                audit_result.get("user_modified") or audit_result.get("missing_from_disk")
            ),
        })

    by_template: Dict[str, Dict[str, Any]] = {}
    for proj in projects:
        key = f"{proj['template_name']} v{proj['template_version']}"
        if key not in by_template:
            by_template[key] = {
                "template_name": proj["template_name"],
                "template_version": proj["template_version"],
                "project_count": 0,
                "needs_sync_count": 0,
                "template_original_total": 0,
                "user_modified_total": 0,
                "user_created_total": 0,
                "missing_from_disk_total": 0,
                "orphaned_on_disk_total": 0,
                "projects": [],
            }
        entry = by_template[key]
        entry["project_count"] += 1
        if proj["needs_sync"]:
            entry["needs_sync_count"] += 1
        entry["template_original_total"] += proj["template_original"]
        entry["user_modified_total"] += proj["user_modified"]
        entry["user_created_total"] += proj["user_created"]
        entry["missing_from_disk_total"] += proj["missing_from_disk"]
        entry["orphaned_on_disk_total"] += proj["orphaned_on_disk"]
        entry["projects"].append(proj)

    return {
        "workspace": str(workspace_path),
        "total_projects": len(projects),
        "needs_sync_projects": sum(1 for p in projects if p["needs_sync"]),
        "by_template": by_template,
        "projects": projects,
    }


def print_workspace_audit_report(ws_audit: Dict[str, Any]) -> None:
    """Print a formatted workspace batch audit report."""
    if "error" in ws_audit:
        print(f"\n  \033[91m{ws_audit['error']}\033[0m\n")
        return

    projects = ws_audit.get("projects", [])
    by_template = ws_audit.get("by_template", {})

    if not projects:
        print("\n  \033[93mNo projects with template-gen manifests found in this workspace.\033[0m\n")
        return

    print(f"\n  \033[1m{'='*60}\033[0m")
    print(f"  \033[1m  Workspace Audit: {ws_audit['workspace']}\033[0m")
    print(f"  \033[1m{'='*60}\033[0m")
    print(f"  Total projects:       {ws_audit['total_projects']}")
    print(f"  Needs sync:           \033[93m{ws_audit['needs_sync_projects']}\033[0m")
    print(f"  Up to date:           \033[92m{ws_audit['total_projects'] - ws_audit['needs_sync_projects']}\033[0m")
    print(f"  Templates detected:   {len(by_template)}")

    for key, entry in sorted(by_template.items()):
        n = entry["project_count"]
        ns = entry["needs_sync_count"]
        to = entry["template_original_total"]
        um = entry["user_modified_total"]
        uc = entry["user_created_total"]
        md = entry["missing_from_disk_total"]
        od = entry["orphaned_on_disk_total"]
        total = to + um + uc + md + od

        status = "\033[92m✓ up to date\033[0m" if ns == 0 else f"\033[93m⚠ {ns}/{n} need sync\033[0m"
        print(f"\n  \033[1m  {key}\033[0m  {status}")
        print(f"    Projects: {n}  |  Files: {total}")
        print(f"    \033[92mTemplate\033[0m: {to}  \033[93mModified\033[0m: {um}  \033[96mUser\033[0m: {uc}  \033[91mMissing\033[0m: {md}  \033[90mOrphaned\033[0m: {od}")
        for proj in entry["projects"]:
            sync_tag = "\033[93m⚠\033[0m" if proj["needs_sync"] else "\033[92m✓\033[0m"
            print(f"      {sync_tag} {proj['project_dir']}")

    print(f"\n  \033[90m{'─'*60}\033[0m")
    print(f"  Export JSON: template-gen audit --workspace <dir> --json\n")


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