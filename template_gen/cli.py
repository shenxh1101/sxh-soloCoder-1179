"""CLI entry point for template-gen using Click."""

import sys
from pathlib import Path
from typing import Optional

import click
import questionary

from template_gen.audit import audit_project, print_audit_report, repair_manifest
from template_gen.config import (
    discover_builtin_templates,
    discover_external_template,
    load_template_config,
)
from template_gen.engine import render_project
from template_gen.interactive import collect_variables
from template_gen.manifest import (
    load_manifest,
    write_manifest,
    update_manifest_after_update,
)
from template_gen.post_gen import execute_post_commands
from template_gen.preset import (
    check_preset_match,
    delete_preset,
    list_presets,
    load_preset,
    save_preset,
)
from template_gen.updater import incremental_update, print_diff_preview
from template_gen.validate_cmd import print_validation_report, validate_template


def _print_banner():
    click.echo()
    click.secho("  ╔══════════════════════════════════╗", fg="cyan")
    click.secho("  ║     Template Generator  v0.2     ║", fg="cyan")
    click.secho("  ╚══════════════════════════════════╝", fg="cyan")
    click.echo()


def _resolve_template(template_name: Optional[str] = None) -> dict:
    builtin = discover_builtin_templates()

    if template_name:
        for t in builtin:
            if t["name"].lower() == template_name.lower():
                return t

        ext = discover_external_template(template_name)
        if ext:
            return ext

        click.secho(f"Template '{template_name}' not found.", fg="red")
        raise SystemExit(1)

    if not builtin:
        click.secho("No built-in templates available.", fg="yellow")
        path_input = click.prompt("Enter path to external template directory")
        ext = discover_external_template(path_input)
        if not ext:
            click.secho(f"No valid template found at '{path_input}'.", fg="red")
            raise SystemExit(1)
        return ext

    choices = []
    for t in builtin:
        label = f"{t['name']} - {t['description']}" if t.get("description") else t["name"]
        choices.append(questionary.Choice(title=label, value=t))

    choices.append(questionary.Choice(title="Browse external template...", value="__external__"))

    selected = questionary.select("Select a template:", choices=choices).unsafe_ask()

    if selected == "__external__":
        path_input = questionary.text("Enter path to external template directory:").unsafe_ask()
        ext = discover_external_template(path_input)
        if not ext:
            click.secho(f"No valid template found at '{path_input}'.", fg="red")
            raise SystemExit(1)
        return ext

    return selected


def _load_presets_interactive(current_template_name: str) -> Optional[dict]:
    """Show presets filtered to match the current template (or warn on mismatch)."""
    all_presets = list_presets()
    if not all_presets:
        return None

    matching = [p for p in all_presets if p["template_name"].lower() == current_template_name.lower()]
    non_matching = [p for p in all_presets if p not in matching]

    if not matching and not non_matching:
        return None

    use_preset = questionary.confirm("Use a saved preset?", default=False).unsafe_ask()
    if not use_preset:
        return None

    choices = []
    if matching:
        for p in matching:
            ver = f" v{p.get('template_version', '?')}"
            choices.append(questionary.Choice(
                title=f"{p['name']} → {p['template_name']}{ver}  \033[92m(match)\033[0m",
                value=p["name"],
            ))

    if non_matching:
        for p in non_matching:
            ver = f" v{p.get('template_version', '?')}"
            choices.append(questionary.Choice(
                title=f"{p['name']} → {p['template_name']}{ver}  \033[93m(different template)\033[0m",
                value=p["name"],
            ))

    choices.append(questionary.Choice(title="Skip, answer manually", value=None))

    preset_name = questionary.select("Select preset:", choices=choices).unsafe_ask()
    if preset_name is None:
        return None

    if preset_name in [p["name"] for p in non_matching]:
        mismatch = check_preset_match(preset_name, current_template_name)
        click.secho(f"\n  ⚠  {mismatch}", fg="yellow")
        proceed = questionary.confirm("Continue with this preset anyway?", default=False).unsafe_ask()
        if not proceed:
            return None

    data = load_preset(preset_name)
    if data is None:
        click.secho(f"Preset '{preset_name}' not found.", fg="red")
        return None

    return data


def _resolve_preset(preset_name: str, template_name: str) -> Optional[dict]:
    """Load a preset by name. Returns None if not found; exits if mismatch and user declines."""
    mismatch = check_preset_match(preset_name, template_name)
    if mismatch:
        click.secho(f"\n  ⚠  {mismatch}", fg="yellow")
        click.secho("  The preset was created for a different template. Variables may not be compatible.\n", fg="yellow")
        proceed = questionary.confirm("Continue anyway?", default=False).unsafe_ask()
        if not proceed:
            raise SystemExit(0)

    data = load_preset(preset_name)
    if data is None:
        click.secho(f"\n  Preset '{preset_name}' not found.", fg="red")
        raise SystemExit(1)

    return data


@click.group()
def main():
    pass


# ── new ─────────────────────────────────────────────────────────────────────

@main.command()
@click.option("-t", "--template", default=None, help="Template name or path")
@click.option("-o", "--output", default=None, help="Output directory")
@click.option("-p", "--preset", default=None, help="Preset name to use")
@click.option("--no-interactive", is_flag=True, help="Use defaults for all variables")
@click.option("--dry-run", is_flag=True, help="Show what would be generated without creating files")
def new(template: Optional[str], output: Optional[str], preset: Optional[str],
        no_interactive: bool, dry_run: bool):
    """Generate a new project from a template."""
    _print_banner()

    template_info = _resolve_template(template)
    config = load_template_config(template_info["config_file"])

    click.secho(f"\n  Template: {config.name}  (v{config.version})", fg="cyan", bold=True)
    if config.description:
        click.echo(f"  {config.description}\n")

    preset_vars = None
    template_vars = {}

    if preset:
        preset_vars = _resolve_preset(preset, config.name).get("variables", {})

    if not preset_vars and not no_interactive:
        preset_data = _load_presets_interactive(config.name)
        if preset_data:
            preset_vars = preset_data.get("variables", {})

    if no_interactive and not preset_vars:
        template_vars = {var.name: var.default for var in config.variables}
    else:
        template_vars = collect_variables(config, presets=preset_vars)

    click.secho("\n  Generating project...", fg="cyan")

    output_dir = output or template_vars.get("project_name", "my_project")
    output_dir = Path(output_dir).resolve()

    if dry_run:
        click.echo(f"\n  [DRY RUN] Would generate into: {output_dir}")
        click.echo(f"  Variables: {template_vars}")
        rendered = {}
    else:
        rendered = render_project(
            template_dir=template_info["path"],
            output_dir=str(output_dir),
            context=template_vars,
        )

        write_manifest(
            project_dir=str(output_dir),
            template_name=config.name,
            template_version=config.version,
            variables=template_vars,
            rendered_files=rendered,
        )

    files_count = len(rendered) if rendered else 0
    click.secho(f"  \033[92mGenerated {files_count} file(s) into {output_dir}\033[0m")

    if not dry_run:
        click.secho(f"  \033[90mManifest: {output_dir}/.template_gen_manifest.json\033[0m")

    if not dry_run and config.post_commands:
        if no_interactive:
            execute_post_commands(config.post_commands, str(output_dir), template_vars)
        else:
            ask_run = questionary.confirm(
                "Run post-generation commands?", default=True
            ).unsafe_ask()
            if ask_run:
                execute_post_commands(config.post_commands, str(output_dir), template_vars)

    if not dry_run and not no_interactive:
        save = questionary.confirm(
            "Save these choices as a preset for later reuse?", default=False
        ).unsafe_ask()
        if save:
            preset_name = questionary.text(
                "Preset name:", default=config.name
            ).unsafe_ask()
            path = save_preset(preset_name, config.name, config.version, template_vars)
            click.secho(f"  Preset saved: {path}", fg="green")

    click.secho("\n  Done!", fg="green", bold=True)


# ── list ────────────────────────────────────────────────────────────────────

@main.command("list")
def list_templates():
    """List available templates."""
    _print_banner()
    builtin = discover_builtin_templates()

    if builtin:
        click.secho("  Built-in templates:", fg="cyan", bold=True)
        for t in builtin:
            click.echo(f"    • {t['name']}: {t['description']}")
    else:
        click.secho("  No built-in templates found.", fg="yellow")


# ── validate ────────────────────────────────────────────────────────────────

@main.command()
@click.option("-t", "--template", default=None, help="Template name or path")
@click.option("--ci", is_flag=True, help="CI mode: exit 0=clean, 1=errors found, 2=warnings only")
@click.option("--json", "json_output", is_flag=True, help="Output validation results as JSON")
def validate(template: Optional[str], ci: bool, json_output: bool):
    """Validate a template configuration. Checks YAML, variables, conditions, rendering, and post-commands."""
    _print_banner()

    template_info = _resolve_template(template)
    config = load_template_config(template_info["config_file"])

    click.secho(f"\n  Validating: {config.name} (v{config.version})", fg="cyan", bold=True)

    issues = validate_template(template_info["path"])
    exit_code = print_validation_report(issues, config.name, json_output=json_output)

    if ci or json_output:
        raise SystemExit(exit_code)


# ── preset ──────────────────────────────────────────────────────────────────

@main.group()
def preset_cmd():
    """Manage saved presets."""
    pass


@preset_cmd.command("save")
@click.argument("name", required=False)
def preset_save(name: Optional[str]):
    """Save a preset (interactive or by name)."""
    if name:
        preset_name = name
    else:
        preset_name = questionary.text("Preset name:").unsafe_ask()
        if not preset_name.strip():
            click.secho("Preset name cannot be empty.", fg="red")
            return

    _print_banner()

    builtin = discover_builtin_templates()
    if not builtin:
        click.secho("No built-in templates found.", fg="yellow")
        path_input = questionary.text("Path to external template:").unsafe_ask()
        ext = discover_external_template(path_input)
        if not ext:
            click.secho("Invalid template path.", fg="red")
            return
        template_info = ext
    else:
        choices = [questionary.Choice(title=f"{t['name']} - {t['description']}", value=t) for t in builtin]
        choices.append(questionary.Choice(title="External template...", value="__external__"))

        selected = questionary.select("Select template:", choices=choices).unsafe_ask()

        if selected == "__external__":
            path_input = questionary.text("Path to template:").unsafe_ask()
            ext = discover_external_template(path_input)
            if not ext:
                click.secho("Invalid template path.", fg="red")
                return
            template_info = ext
        else:
            template_info = selected

    config = load_template_config(template_info["config_file"])
    click.secho(f"\n  Template: {config.name} v{config.version}", fg="cyan")

    existing = list_presets()
    if any(p["name"] == preset_name for p in existing):
        overwrite = questionary.confirm(
            f"Preset '{preset_name}' already exists. Overwrite?", default=False
        ).unsafe_ask()
        if not overwrite:
            return

    variables = collect_variables(config)
    path = save_preset(preset_name, config.name, config.version, variables)
    click.secho(f"\n  \033[92mPreset '{preset_name}' saved.\033[0m")
    click.secho(f"  Template: {config.name} v{config.version}")
    click.secho(f"  Path: {path}")


@preset_cmd.command("list")
def preset_list():
    """List saved presets."""
    presets = list_presets()
    if not presets:
        click.secho("No presets saved yet.", fg="yellow")
        click.secho("Use 'template-gen preset save <name>' to create one.")
        return

    click.echo()
    click.secho("  Saved presets:", fg="cyan", bold=True)
    for p in presets:
        ver = f" v{p['template_version']}" if p.get("template_version") else ""
        click.echo(f"    • \033[1m{p['name']}\033[0m  →  {p['template_name']}{ver}")


@preset_cmd.command("show")
@click.argument("name")
def preset_show(name: str):
    """Show the contents of a preset."""
    data = load_preset(name)
    if not data:
        click.secho(f"Preset '{name}' not found.", fg="red")
        return

    click.echo()
    click.secho(f"  Preset: \033[1m{data['name']}\033[0m", fg="cyan")
    click.secho(f"  Template: {data.get('template_name', 'N/A')}  v{data.get('template_version', '?')}")
    click.secho(f"  Variables ({len(data.get('variables', {}))}):", fg="cyan")
    for k, v in data.get("variables", {}).items():
        click.echo(f"    {k} = {v}")


@preset_cmd.command("delete")
@click.argument("name")
def preset_delete(name: str):
    """Delete a saved preset."""
    if delete_preset(name):
        click.secho(f"Preset '{name}' deleted.", fg="green")
    else:
        click.secho(f"Preset '{name}' not found.", fg="red")


# ── diff ────────────────────────────────────────────────────────────────────

@main.command()
@click.argument("project_dir")
@click.option("-t", "--template", required=True, help="Template name or path")
@click.option("--no-interactive", is_flag=True, help="Use defaults for all variables")
def diff(project_dir: str, template: str, no_interactive: bool):
    """Preview changes between current project and updated template without applying them."""
    _print_banner()

    project_path = Path(project_dir).resolve()
    if not project_path.exists():
        click.secho(f"Project directory not found: {project_dir}", fg="red")
        raise SystemExit(1)

    manifest = load_manifest(str(project_path))
    template_info = _resolve_template(template)
    config = load_template_config(template_info["config_file"])

    if manifest:
        click.secho(f"  Manifest found: {manifest.get('template_name')} v{manifest.get('template_version')}", fg="cyan")

    click.secho(f"  Comparing with: {config.name} v{config.version}", fg="cyan")

    preset_vars = None
    if manifest and manifest.get("variables"):
        if not no_interactive:
            reuse = questionary.confirm(
                "Reuse variables from existing manifest?", default=True
            ).unsafe_ask()
            if reuse:
                preset_vars = manifest.get("variables", {})

    if not preset_vars and not no_interactive:
        preset_data = _load_presets_interactive(config.name)
        if preset_data:
            preset_vars = preset_data.get("variables", {})

    if no_interactive and not preset_vars:
        template_vars = {var.name: var.default for var in config.variables}
    else:
        template_vars = collect_variables(config, presets=preset_vars)

    rendered = render_project(
        template_dir=template_info["path"],
        output_dir="",
        context=template_vars,
        dry_run=True,
    )

    print_diff_preview(str(project_path), rendered, config.name, config.version)
    click.secho("  Run 'template-gen update' to apply these changes.", fg="cyan", bold=True)


# ── audit ───────────────────────────────────────────────────────────────────

@main.command()
@click.argument("project_dir")
@click.option("--repair", is_flag=True, help="Repair the manifest: remove missing entries, add orphaned files")
@click.option("--json", "json_output", is_flag=True, help="Output audit results as JSON")
def audit(project_dir: str, repair: bool, json_output: bool):
    """Audit a generated project: report file states and manifest consistency."""
    _print_banner()

    project_path = Path(project_dir).resolve()
    if not project_path.exists():
        click.secho(f"Project directory not found: {project_dir}", fg="red")
        raise SystemExit(1)

    if repair:
        result = repair_manifest(str(project_path))
        if result["repaired"]:
            click.secho(f"\n  \033[92mManifest repaired.\033[0m", fg="green")
            click.secho(f"  Removed {result['removed_missing']} missing entries")
            click.secho(f"  Added {result['added_orphaned']} orphaned files as user_created")
        else:
            click.secho(f"\n  \033[93m{result.get('reason', 'Repair failed')}\033[0m", fg="yellow")
        return

    audit_data = audit_project(str(project_path))

    if json_output:
        import json
        audit_data.pop("has_manifest", None)
        print(json.dumps(audit_data, indent=2, ensure_ascii=False, default=str))
    else:
        print_audit_report(audit_data)


# ── update ──────────────────────────────────────────────────────────────────

@main.command()
@click.argument("project_dir")
@click.option("-t", "--template", required=True, help="Template name or path")
@click.option("--no-backup", is_flag=True, help="Skip creating a backup before updating")
@click.option("--no-interactive", is_flag=True, help="Apply all changes without prompting")
@click.option("--dry-run", is_flag=True, help="Show what would change without applying")
def update(project_dir: str, template: str, no_backup: bool, no_interactive: bool, dry_run: bool):
    """Incrementally update an existing project from its template."""
    _print_banner()

    project_path = Path(project_dir).resolve()
    if not project_path.exists():
        click.secho(f"Project directory not found: {project_dir}", fg="red")
        raise SystemExit(1)

    manifest = load_manifest(str(project_path))
    template_info = _resolve_template(template)
    config = load_template_config(template_info["config_file"])

    if manifest:
        click.secho(f"\n  Existing: {manifest.get('template_name')} v{manifest.get('template_version')}", fg="cyan")

    click.secho(f"  Updating from: {config.name} v{config.version}", fg="cyan")

    preset_vars = None
    if manifest and manifest.get("variables"):
        if not no_interactive:
            reuse = questionary.confirm(
                "Reuse variables from existing manifest?", default=True
            ).unsafe_ask()
            if reuse:
                preset_vars = manifest.get("variables", {})
                click.secho("  Using variables from existing manifest.", fg="green")

    if not preset_vars and not no_interactive:
        preset_data = _load_presets_interactive(config.name)
        if preset_data:
            preset_vars = preset_data.get("variables", {})

    if no_interactive and not preset_vars:
        template_vars = {var.name: var.default for var in config.variables}
    else:
        template_vars = collect_variables(config, presets=preset_vars)

    click.secho("\n  Computing template output...", fg="cyan")
    rendered = render_project(
        template_dir=template_info["path"],
        output_dir="",
        context=template_vars,
        dry_run=True,
    )

    result = incremental_update(
        project_dir=str(project_path),
        new_files=rendered,
        backup=not no_backup,
        dry_run=dry_run,
        interactive=not no_interactive,
    )

    if not dry_run and (result["changed"] or result["added"] or result["skipped"] or result.get("resolved_conflicts")):
        classification = result.get("classifications", {})
        update_manifest_after_update(
            project_dir=str(project_path),
            template_name=config.name,
            template_version=config.version,
            variables=template_vars,
            new_render=rendered,
            classifications={
                "unchanged": result.get("unchanged", []),
                "changed": result.get("changed", []),
                "template_new": result.get("added", []),
                "template_removed": result.get("removed", []),
                "user_only": result.get("user_only", []),
                "skipped": result.get("skipped", []),
            },
            resolved_conflicts=result.get("resolved_conflicts", []),
        )

    if result["backup"]:
        click.secho(f"\n  Backup: {result['backup']}", fg="cyan")

    click.secho("  Update complete!", fg="green")


if __name__ == "__main__":
    main()