"""Interactive Q&A engine with conditional logic and preset support."""

import questionary
from jinja2 import Environment, BaseLoader, UndefinedError
from typing import Any, Dict, List, Optional

from template_gen.config import TemplateConfig, TemplateVariable
from template_gen.validator import validate_variable


class SilentUndefined:
    """An undefined-like object that returns empty string for any attribute access."""

    def __getattr__(self, name: str) -> str:
        return ""

    def __str__(self) -> str:
        return ""

    def __bool__(self) -> bool:
        return False


def _condition_met(condition: Optional[str], context: Dict[str, Any]) -> bool:
    if not condition:
        return True
    try:
        env = Environment(loader=BaseLoader(), undefined=SilentUndefined)
        template = env.from_string("{{% if {} %}}1{{% else %}}0{{% endif %}}".format(condition))
        result = template.render(**context).strip()
        return result == "1"
    except Exception:
        return False


def _coerce_value(var: TemplateVariable, raw: Any) -> Any:
    if var.kind == "confirm":
        return bool(raw)
    if var.kind == "int":
        return int(raw)
    if var.kind == "float":
        return float(raw)
    return str(raw) if raw is not None else ""


def _ask_variable(var: TemplateVariable, context: Dict[str, Any]) -> Any:
    default_val = var.default
    choices_list = var.choices or (var.validation.choices if var.validation else None)

    if var.kind == "confirm":
        default_bool = default_val if isinstance(default_val, bool) else str(default_val).lower() in ("true", "yes", "1")
        return questionary.confirm(var.prompt, default=default_bool).unsafe_ask()

    if var.kind == "select" and choices_list:
        choice_map = {}
        default_title = None
        default_str = str(default_val)
        for c in choices_list:
            title = c.title if hasattr(c, "title") else str(c.value)
            value = c.value if hasattr(c, "value") else c
            choice_map[title] = value
            if str(value) == default_str:
                default_title = title

        selected = questionary.select(
            var.prompt,
            choices=list(choice_map.keys()),
            default=default_title,
        ).unsafe_ask()
        return choice_map[selected]

    if var.kind == "password":
        return questionary.password(var.prompt).unsafe_ask()

    default_display = str(default_val) if default_val != "" else ""
    raw = questionary.text(var.prompt, default=default_display).unsafe_ask()
    return raw


def collect_variables(
    config: TemplateConfig,
    presets: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    context: Dict[str, Any] = {}
    presets = presets or {}

    for var in config.variables:
        if not _condition_met(var.condition, context):
            continue

        if var.name in presets:
            value = presets[var.name]
            err = validate_variable(var, value)
            if err is None:
                context[var.name] = _coerce_value(var, value)
                continue

        value = _ask_variable(var, context)

        while True:
            err = validate_variable(var, value)
            if err is None:
                break
            print(f"  \033[91mValidation error: {err}\033[0m")
            value = _ask_variable(var, context)

        context[var.name] = _coerce_value(var, value)

    return context