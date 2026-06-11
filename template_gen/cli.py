"""CLI entry point for template-gen using Click."""

from pathlib import Path
from typing import Optional

import click
import questionary

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
from template_gen.updater import incremental_update
from template_gen.validate_cmd import print_validation_report, validate_template


def _print_banner():
    click.echo()
    click.secho("  ╔══════════════════════════════════╗", fg="cyan")
    click.secho("  ║     Template Generator  v0.1     ║", fg="cyan")
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

    selected = questionary.select(
        "Select a template:",
        choices=choices,
    ).unsafe_ask()

    if selected == "__external__":
        path_input = questionary.text("Enter path to external template directory:").unsafe_ask()
        ext = discover_external_template(path_input)
        if not ext:
            click.secho(f"No valid template found at '{path_input}'.", fg="red")
            raise SystemExit(1)
        return ext

    return selected


def _load_presets_interactive(template_name: str) -> Optional[dict]:
    presets = list_presets()
    if not presets:
        return None

    use_preset = questionary.confirm("Use a saved preset?", default=False).unsafe_ask()
    if not use_preset:
        return None

    choices = []
    for p in presets:
        label = f"{p['name']} ({p['template_name']} v{p.get('template_version', '?')})"
        choices.append(questionary.Choice(title=label, value=p["name"]))
    choices.append(questionary.Choice(title="Skip, answer manually", value=None))

    preset_name = questionary.select("Select preset:", choices=choices).unsafe_ask()
    if preset_name is None:
        return None

    mismatch = check_preset_match(preset_name, template_name)
    if mismatch:
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
    data = load_preset(preset_name)
    if data is None:
        click.secho(f"\n  Preset '{preset_name}' not found.", fg="red")
        return None

    mismatch = check_preset_match(preset_name, template_name)
    if mismatch:
        click.secho(f"\n  ⚠  {mismatch}", fg="yellow")
        proceed = questionary.confirm("Continue with this preset anyway?", default=False).unsafe_ask()
        if not proceed:
            raise SystemExit(0)

    return data


@click.group()
def main():
    pass


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
        preset_data = _resolve_preset(preset, config.name)
        if preset_data:
            preset_vars = preset_data.get("variables", {})
            click.secho(f"  Using preset: {preset}", fg="green")

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


@main.command()
@click.option("-t", "--template", default=None, help="Template name or path")
@click.option("--fix", is_flag=True, help="Attempt to auto-fix issues")
def validate(template: Optional[str], fix: bool):
    """Validate a template configuration. Checks YAML, variables, conditions, rendering, and post-commands."""
    _print_banner()

    template_info = _resolve_template(template)
    config = load_template_config(template_info["config_file"])

    click.secho(f"\n  Validating: {config.name} (v{config.version})", fg="cyan", bold=True)

    issues = validate_template(template_info["path"])
    exit_code = print_validation_report(issues, config.name)

    raise SystemExit(exit_code)


@main.group()
def preset_cmd():
    """Manage presets."""
    pass


@preset_cmd.command("save")
@click.argument("name")
def preset_save(name: str):
    """Save a preset (interactive)."""
    builtin = discover_builtin_templates()
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
    variables = collect_variables(config)
    path = save_preset(name, config.name, config.version, variables)
    click.secho(f"Preset saved: {path}", fg="green")


@preset_cmd.command("list")
def preset_list():
    """List saved presets."""
    presets = list_presets()
    if not presets:
        click.secho("No presets saved.", fg="yellow")
        return

    click.secho("Saved presets:", fg="cyan", bold=True)
    for p in presets:
        ver = f" v{p['template_version']}" if p.get("template_version") else ""
        click.echo(f"  • {p['name']} → {p['template_name']}{ver}")


@preset_cmd.command("delete")
@click.argument("name")
def preset_delete(name: str):
    """Delete a saved preset."""
    if delete_preset(name):
        click.secho(f"Preset '{name}' deleted.", fg="green")
    else:
        click.secho(f"Preset '{name}' not found.", fg="red")


@preset_cmd.command("show")
@click.argument("name")
def preset_show(name: str):
    """Show the contents of a preset."""
    data = load_preset(name)
    if not data:
        click.secho(f"Preset '{name}' not found.", fg="red")
        return

    click.secho(f"Preset: {data['name']}", fg="cyan", bold=True)
    click.secho(f"Template: {data.get('template_name', 'N/A')}  v{data.get('template_version', '?')}", fg="cyan")
    click.secho("Variables:", fg="cyan")
    for k, v in data.get("variables", {}).items():
        click.echo(f"  {k} = {v}")


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
        existing_name = manifest.get("template_name", "")
        existing_ver = manifest.get("template_version", "")
        click.secho(f"\n  Existing project was generated from: {existing_name} v{existing_ver}", fg="cyan")

    click.secho(f"  Updating from template: {config.name} v{config.version}", fg="cyan")

    preset_vars = None
    if manifest and manifest.get("variables"):
        reuse = not no_interactive and questionary.confirm(
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

    if not dry_run and (result["changed"] or result["added"]):
        update_manifest_after_update(
            project_dir=str(project_path),
            template_name=config.name,
            template_version=config.version,
            variables=template_vars,
            new_render=rendered,
            classifications={
                "unchanged": result.get("unchanged", []),
                "changed": result["changed"],
                "template_new": result["added"],
                "template_removed": result.get("removed", []),
                "user_only": result.get("user_only", []),
            },
        )

    if result["backup"]:
        click.secho(f"\n  Backup: {result['backup']}", fg="cyan")

    click.secho("\n  Update complete!", fg="green")


if __name__ == "__main__":
    main()