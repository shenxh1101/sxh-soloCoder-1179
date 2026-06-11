"""Template validator: checks YAML config, conditions, renderability, and post-commands.

Exit codes (for CI):
  0 = no issues
  1 = errors found
  2 = only warnings found
"""

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
        self.level = level   # error, warning
        self.category = category   # yaml, variable, condition, template, post_command, reference
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
    issues.extend(_validate_variable_usage(template_dir, config))

    return issues


# ── variable definitions ────────────────────────────────────────────────────

def _validate_variable_definitions(config: TemplateConfig) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []

    for var in config.variables:
        if var.kind not in ("text", "confirm", "select", "password", "int", "float"):
            issues.append(ValidationIssue(
                "warning", "variable",
                f"'{var.name}' has unknown kind '{var.kind}'",
            ))

        if var.kind == "select" and not var.choices and not (var.validation and var.validation.choices):
            issues.append(ValidationIssue(
                "warning", "variable",
                f"'{var.name}' is kind='select' but has no choices defined",
            ))

        if var.kind in ("int", "float") and var.validation and var.validation.kind == "range":
            msg = validate_variable(var, var.default)
            if msg:
                issues.append(ValidationIssue(
                    "error", "variable",
                    f"'{var.name}' default value '{var.default}' fails its own validation: {msg}",
                ))

        if var.validation and var.validation.kind == "regex":
            try:
                re.compile(var.validation.pattern or "")
            except re.error:
                issues.append(ValidationIssue(
                    "error", "variable",
                    f"'{var.name}' has invalid regex pattern: {var.validation.pattern}",
                ))

        if var.kind in ("int", "float") and isinstance(var.default, str) and var.default == "":
            msg = validate_variable(var, var.default)
            if msg:
                issues.append(ValidationIssue(
                    "warning", "variable",
                    f"'{var.name}' has empty string as default but is kind='{var.kind}', will fail validation at prompt",
                ))

    return issues


# ── condition expressions ───────────────────────────────────────────────────

def _validate_conditions(config: TemplateConfig) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    env = Environment(loader=BaseLoader())

    declared_vars = {var.name for var in config.variables}
    context: Dict[str, Any] = {}
    for var in config.variables:
        context[var.name] = var.default

    for var in config.variables:
        if not var.condition:
            continue

        cond_issues = _check_condition_expr(env, var.condition, context, declared_vars, f"variable '{var.name}'")
        issues.extend(cond_issues)

    for i, cmd in enumerate(config.post_commands):
        if not cmd.condition:
            continue

        label = f"post-command '{cmd.description or cmd.command}'"
        cond_issues = _check_condition_expr(env, cmd.condition, context, declared_vars, label)
        issues.extend(cond_issues)

    return issues


def _check_condition_expr(
    env: Environment,
    condition: str,
    context: Dict[str, Any],
    declared_vars: set,
    label: str,
) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []

    try:
        tmpl = env.from_string(
            "{{% if {} %}}1{{% else %}}0{{% endif %}}".format(condition)
        )
        tmpl.render(**context)
    except Exception as e:
        issues.append(ValidationIssue(
            "error", "condition",
            f"Condition on {label} is invalid: '{condition}'",
            detail=str(e),
        ))

    refs = _extract_jinja2_expression_refs(condition)
    for ref_name in refs:
        if ref_name not in declared_vars:
            issues.append(ValidationIssue(
                "error", "condition",
                f"Condition on {label} references undeclared variable '{ref_name}': '{condition}'",
            ))

    return issues


def _extract_jinja2_expression_refs(expr: str) -> set:
    """Extract variable names from a Jinja2 expression like 'use_docker == true'."""
    return set(re.findall(r"\b([a-zA-Z_]\w*)\b", expr)) - {
        "true", "false", "True", "False", "None", "none",
        "and", "or", "not", "is", "in",
        "if", "else", "elif", "for", "while",
        "def", "class", "import", "from", "as", "with",
        "try", "except", "finally", "raise", "return",
        "yield", "lambda", "pass", "break", "continue",
        "len", "int", "str", "float", "bool", "list", "dict",
        "set", "tuple", "type", "range", "print", "hasattr",
    }


# ── template rendering ──────────────────────────────────────────────────────

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
            "Template subdirectory 'template/' not found",
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
            f"Failed to render template files: {e}",
        ))
        return issues

    for rel_path, content in rendered.items():
        if content.startswith("[ERROR rendering"):
            issues.append(ValidationIssue(
                "error", "template",
                f"Render error in '{rel_path}'",
                detail=content,
            ))

    return issues


# ── variable usage analysis ─────────────────────────────────────────────────

def _validate_variable_usage(
    template_dir: str, config: TemplateConfig
) -> List[ValidationIssue]:
    """Check: which variables are unused, and which template refs are undeclared."""
    issues: List[ValidationIssue] = []

    template_subdir = os.path.join(template_dir, "template")
    if not os.path.isdir(template_subdir):
        return issues

    all_sources = _read_all_template_sources(template_subdir)
    declared_vars = {var.name for var in config.variables}

    for var in config.variables:
        ref_count = _count_var_refs_in_source(all_sources, var.name)
        if ref_count == 0:
            issues.append(ValidationIssue(
                "warning", "reference",
                f"Variable '{var.name}' is declared but never referenced in any template file",
            ))

    all_refs = _extract_all_template_refs(all_sources)
    for ref_name in sorted(all_refs):
        if ref_name not in declared_vars:
            if _has_default_filter(all_sources, ref_name):
                issues.append(ValidationIssue(
                    "warning", "reference",
                    f"Template references '{ref_name}' which is not declared (uses default filter → fallback value)",
                ))
            else:
                issues.append(ValidationIssue(
                    "error", "reference",
                    f"Template references undeclared variable '{{{{ {ref_name} }}}}' — will be empty at render time",
                ))

    return issues


def _read_all_template_sources(template_subdir: str) -> str:
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


def _count_var_refs_in_source(all_sources: str, var_name: str) -> int:
    count = 0
    for m in re.finditer(
        r"\{\{\s*" + re.escape(var_name) + r"(\s*\||\s*\}\}|\s*\})", all_sources
    ):
        count += 1
    for m in re.finditer(
        r"\{\%\s*(?:if|elif|for)\s+.*\b" + re.escape(var_name) + r"\b", all_sources
    ):
        count += 1
    return count


def _has_default_filter(all_sources: str, var_name: str) -> bool:
    return bool(re.search(
        r"\{\{\s*" + re.escape(var_name) + r"\s*\|",
        all_sources,
    ))


def _extract_all_template_refs(content: str) -> set:
    refs = set()
    for match in re.finditer(r"\{\{\s*(\w+)\s*(\|[^}]*)?\}\}", content):
        refs.add(match.group(1))
    for match in re.finditer(r"\{\%\s*(?:if|elif|for)\s+.*?\b(\w+)\b", content):
        refs.add(match.group(1))
    return refs - {
        "true", "false", "True", "False", "None",
        "and", "or", "not", "is", "in", "if", "else",
    }


# ── post commands ───────────────────────────────────────────────────────────

def _validate_post_commands(config: TemplateConfig) -> List[ValidationIssue]:
    issues: List[ValidationIssue] = []
    env = Environment(loader=BaseLoader())
    context = {var.name: var.default for var in config.variables}

    for i, cmd in enumerate(config.post_commands):
        try:
            env.from_string(cmd.command).render(**context)
        except Exception as e:
            issues.append(ValidationIssue(
                "error", "post_command",
                f"Post-command #{i+1} has invalid Jinja2 in command string: {cmd.command}",
                detail=str(e),
            ))

        if cmd.condition:
            try:
                env.from_string(
                    "{{% if {} %}}1{{% else %}}0{{% endif %}}".format(cmd.condition)
                ).render(**context)
            except Exception:
                pass

    return issues


# ── report printer ──────────────────────────────────────────────────────────

def print_validation_report(issues: List[ValidationIssue], template_name: str) -> int:
    """Print a formatted validation report grouped by category.
    Returns exit code: 0=clean, 1=errors, 2=warnings only.
    """
    if not issues:
        print(f"\n  \033[92m✓ Template '{template_name}' passed all checks.\033[0m\n")
        return 0

    errors = [i for i in issues if i.level == "error"]
    warnings = [i for i in issues if i.level == "warning"]

    print(f"\n  Template: \033[1m{template_name}\033[0m")
    print(f"  \033[91m{len(errors)} error(s)\033[0m  \033[93m{len(warnings)} warning(s)\033[0m")

    _print_section("Errors", errors, "error")
    _print_section("Warnings", warnings, "warning")

    print()

    if errors:
        return 1
    return 2


def _print_section(title: str, items: List[ValidationIssue], level: str) -> None:
    if not items:
        return

    icon = "\033[91m✗\033[0m" if level == "error" else "\033[93m⚠\033[0m"

    by_category: Dict[str, List[ValidationIssue]] = {}
    for i in items:
        by_category.setdefault(i.category, []).append(i)

    print(f"\n  {icon} {title} ({len(items)}) {'─' * 40}")

    for category in ("yaml", "variable", "condition", "template", "reference", "post_command"):
        cat_issues = by_category.get(category, [])
        if not cat_issues:
            continue
        cat_label = _CATEGORY_LABELS.get(category, category)
        print(f"    [{cat_label}]")
        for issue in cat_issues:
            print(f"      • {issue.message}")
            if issue.detail:
                for line in issue.detail.strip().split("\n")[:3]:
                    print(f"        \033[90m{line.strip()}\033[0m")


_CATEGORY_LABELS = {
    "yaml": "Config YAML",
    "variable": "Variable Definitions",
    "condition": "Condition Expressions",
    "template": "Template Rendering",
    "reference": "Variable References",
    "post_command": "Post Commands",
}