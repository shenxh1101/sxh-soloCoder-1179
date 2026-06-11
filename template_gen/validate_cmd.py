"""Template validator: checks YAML config, conditions, renderability, and post-commands."""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from jinja2 import Environment, BaseLoader

from template_gen.config import (
    TemplateConfig,
    TemplateVariable,
    load_template_config,
)
from template_gen.engine import render_project
from template_gen.validator import validate_variable


class ValidationIssue:
    def __init__(self, level: str, category: str, message: str, detail: str = ""):
        self.level = level  # error, warning
        self.category = category  # yaml, variable, condition, template, post_command
        self.message = message
        self.detail = detail


def validate_template(template_dir: str) -> List[ValidationIssue]:
    """Run all validations on a template. Returns a list of issues found."""
    issues: List[ValidationIssue] = []

    config_path = os.path.join(template_dir, "projgen.yaml")
    if not os.path.exists(config_path):
        issues.append(ValidationIssue("error", "yaml", f"projgen.yaml not found in {template_dir}"))
        return issues

    try:
        config = load_template_config(config_path)
    except Exception as e:
        issues.append(ValidationIssue("error", "yaml", f"Failed to parse projgen.yaml: {e}"))
        return issues

    issues.extend(_validate_variable_definitions(config))
    issues.extend(_validate_conditions(config))
    issues.extend(_validate_template_rendering(template_dir, config))
    issues.extend(_validate_post_commands(config))

    return issues


def _validate_variable_definitions(config: TemplateConfig) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []

    for var in config.variables:
        if var.kind not in ("text", "confirm", "select", "password", "int", "float"):
            issues.append(ValidationIssue(
                "warning", "variable",
                f"Variable '{var.name}' has unknown kind '{var.kind}'"
            ))

        if var.kind == "select" and not var.choices and not (var.validation and var.validation.choices):
            issues.append(ValidationIssue(
                "warning", "variable",
                f"Variable '{var.name}' is kind='select' but has no choices defined"
            ))

        if var.kind in ("int", "float") and var.validation and var.validation.kind == "range":
            v = var.validation
            msg = validate_variable(var, var.default)
            if msg:
                issues.append(ValidationIssue(
                    "error", "variable",
                    f"Variable '{var.name}' default value '{var.default}' fails validation: {msg}"
                ))

        if var.validation and var.validation.kind == "regex":
            import re as _re
            try:
                _re.compile(var.validation.pattern or "")
            except _re.error:
                issues.append(ValidationIssue(
                    "error", "variable",
                    f"Variable '{var.name}' has invalid regex pattern: {var.validation.pattern}"
                ))

    return issues


def _validate_conditions(config: TemplateConfig) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    env = Environment(loader=BaseLoader())

    context: Dict[str, Any] = {}
    for var in config.variables:
        if var.name not in context:
            context[var.name] = var.default

    for var in config.variables:
        if not var.condition:
            continue

        try:
            cond = env.from_string(
                "{{% if {} %}}1{{% else %}}0{{% endif %}}".format(var.condition)
            )
            cond.render(**context)
        except Exception as e:
            issues.append(ValidationIssue(
                "error", "condition",
                f"Variable '{var.name}' has invalid condition expression: {var.condition}",
                detail=str(e),
            ))

    for i, cmd in enumerate(config.post_commands):
        if not cmd.condition:
            continue
        try:
            cond = env.from_string(
                "{{% if {} %}}1{{% else %}}0{{% endif %}}".format(cmd.condition)
            )
            cond.render(**context)
        except Exception as e:
            issues.append(ValidationIssue(
                "error", "condition",
                f"Post-command #{i+1} ('{cmd.description or cmd.command}') has invalid condition: {cmd.condition}",
                detail=str(e),
            ))

    return issues


def _validate_template_rendering(
    template_dir: str, config: TemplateConfig
) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []

    context = {}
    for var in config.variables:
        context[var.name] = var.default

    template_subdir = os.path.join(template_dir, "template")
    if not os.path.isdir(template_subdir):
        issues.append(ValidationIssue(
            "error", "template",
            f"Template subdirectory 'template/' not found in {template_dir}"
        ))
        return issues

    try:
        rendered = render_project(
            template_dir=template_dir,
            output_dir="",
            context=context,
            dry_run=True,
        )
    except Exception as e:
        issues.append(ValidationIssue(
            "error", "template",
            f"Failed to render template files: {e}"
        ))
        return issues

    for rel_path, content in rendered.items():
        if content.startswith("[ERROR rendering"):
            issues.append(ValidationIssue(
                "error", "template",
                f"Render error in '{rel_path}'",
                detail=content,
            ))

    all_raw = _read_all_template_sources(template_subdir)

    context_vars = set(var.name for var in config.variables)

    for var in config.variables:
        ref_count = _count_var_refs(all_raw, var.name)
        if ref_count == 0:
            issues.append(ValidationIssue(
                "warning", "template",
                f"Variable '{var.name}' is defined but never referenced in any template file"
            ))

    all_refs = _extract_all_template_refs(all_raw)
    for ref_name in sorted(all_refs):
        if ref_name not in context_vars:
            if _has_default_filter(all_raw, ref_name):
                issues.append(ValidationIssue(
                    "warning", "template",
                    f"Template references variable '{{{{ {ref_name} }}}}' which is not defined (has default filter, will use fallback)"
                ))
            else:
                issues.append(ValidationIssue(
                    "error", "template",
                    f"Template references undefined variable '{{{{ {ref_name} }}}}'"
                ))

    return issues


def _read_all_template_sources(template_subdir: str) -> str:
    """Read all template source files into one string for analysis."""
    sources = []
    for root, dirs, files in os.walk(template_subdir):
        for fname in files:
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    sources.append(f.read())
            except Exception:
                pass
    return "\n".join(sources)


def _count_var_refs(all_sources: str, var_name: str) -> int:
    import re
    count = 0
    for m in re.finditer(r"\{\{\s*" + re.escape(var_name) + r"(\s*\||\s*\}\}|\s*\})", all_sources):
        count += 1
    for m in re.finditer(r"\{\%\s*(?:if|elif|for)\s+.*\b" + re.escape(var_name) + r"\b", all_sources):
        count += 1
    return count


def _has_default_filter(all_sources: str, var_name: str) -> bool:
    import re
    return bool(re.search(
        r"\{\{\s*" + re.escape(var_name) + r"\s*\|",
        all_sources,
    ))


def _validate_post_commands(config: TemplateConfig) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    env = Environment(loader=BaseLoader())
    context = {var.name: var.default for var in config.variables}

    for i, cmd in enumerate(config.post_commands):
        try:
            rendered = env.from_string(cmd.command).render(**context)
        except Exception as e:
            issues.append(ValidationIssue(
                "error", "post_command",
                f"Post-command #{i+1} has invalid Jinja2 in command: {cmd.command}",
                detail=str(e),
            ))

        if cmd.condition:
            try:
                rendered = env.from_string(
                    "{{% if {} %}}1{{% else %}}0{{% endif %}}".format(cmd.condition)
                ).render(**context)
            except Exception:
                pass

    return issues


def _find_jinja2_refs(rendered_files: Dict[str, str], var_name: str) -> List[str]:
    refs = []
    for path, content in rendered_files.items():
        if "{{%s}}" % var_name in content or "{{ %s }}" % var_name in content:
            refs.append(path)
    return refs


def _extract_all_template_refs(content: str) -> set:
    import re
    refs = set()
    for match in re.finditer(r"\{\{\s*(\w+)\s*(\|[^}]*)?\}\}", content):
        refs.add(match.group(1))
    for match in re.finditer(r"\{\%\s*(?:if|elif|for)\s+(\w+)", content):
        refs.add(match.group(1))
    return refs


def print_validation_report(issues: List[ValidationIssue], template_name: str) -> int:
    """Print a formatted validation report. Returns exit code (0 = ok, 1 = errors)."""
    if not issues:
        print(f"\n  \033[92m✓ Template '{template_name}' passed all checks.\033[0m")
        return 0

    errors = [i for i in issues if i.level == "error"]
    warnings = [i for i in issues if i.level == "warning"]

    print(f"\n  Template: \033[1m{template_name}\033[0m")
    print(f"  \033[91m{len(errors)} error(s)\033[0m  \033[93m{len(warnings)} warning(s)\033[0m")

    for issue in issues:
        icon = "\033[91m✗\033[0m" if issue.level == "error" else "\033[93m⚠\033[0m"
        print(f"\n  {icon} [{issue.category}] {issue.message}")
        if issue.detail:
            for line in issue.detail.strip().split("\n")[:5]:
                print(f"    \033[90m{line}\033[0m")

    return 1 if errors else 0