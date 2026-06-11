"""Variable validation engine supporting regex, range, choice, and required checks."""

import re
from typing import Any, Optional

from template_gen.config import TemplateVariable, VariableValidation


class ValidationError(Exception):
    pass


def validate_variable(var: TemplateVariable, value: Any) -> Optional[str]:
    """Validate a value against a variable's validation rules. Returns error message or None."""
    if var.validation is None:
        return _validate_by_kind(var, value)

    v = var.validation

    if v.kind == "required":
        err = _validate_required(value)
        if err:
            return err

    if v.kind == "regex" and v.pattern:
        err = _validate_regex(value, v.pattern)
        if err:
            return v.message or err

    if v.kind == "range":
        err = _validate_range(value, v.min_value, v.max_value)
        if err:
            return v.message or err

    if v.kind == "choice":
        err = _validate_choice(value, v.choices)
        if err:
            return v.message or err

    err = _validate_by_kind(var, value)
    if err:
        return err

    return None


def _validate_by_kind(var: TemplateVariable, value: Any) -> Optional[str]:
    kind = var.kind

    if kind in ("int",):
        try:
            int(value)
        except (ValueError, TypeError):
            return f"Value must be an integer, got: {value}"

    if kind == "float":
        try:
            float(value)
        except (ValueError, TypeError):
            return f"Value must be a number, got: {value}"

    if kind == "confirm":
        if not isinstance(value, bool):
            return f"Value must be a boolean, got: {value}"

    return None


def _validate_required(value: Any) -> Optional[str]:
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return "This field is required"
    return None


def _validate_regex(value: Any, pattern: str) -> Optional[str]:
    try:
        compiled = re.compile(pattern)
    except re.error as e:
        return f"Invalid regex pattern: {e}"
    if not compiled.match(str(value)):
        return f"Value '{value}' does not match pattern '{pattern}'"
    return None


def _validate_range(
    value: Any, min_value: Optional[float], max_value: Optional[float]
) -> Optional[str]:
    try:
        num = float(value)
    except (ValueError, TypeError):
        return f"Value must be a number, got: {value}"

    if min_value is not None and num < min_value:
        return f"Value must be >= {min_value}, got: {num}"
    if max_value is not None and num > max_value:
        return f"Value must be <= {max_value}, got: {num}"
    return None


def _validate_choice(value: Any, choices: Optional[list]) -> Optional[str]:
    if choices is None:
        return None
    valid_values = set()
    labels = []
    for c in choices:
        v = c.value if hasattr(c, "value") else c
        label = c.title if hasattr(c, "title") else str(c)
        valid_values.add(v)
        labels.append(f"'{label}'")

    if value not in valid_values:
        return f"Value must be one of: {', '.join(labels)}"
    return None