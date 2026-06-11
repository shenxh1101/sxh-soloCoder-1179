"""YAML config parser for template definitions."""

import os
import yaml
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


class VariableChoice(BaseModel):
    title: str
    value: Any


class VariableValidation(BaseModel):
    kind: str = "regex"  # regex, range, choice, required
    pattern: Optional[str] = None
    message: Optional[str] = None
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    choices: Optional[List[VariableChoice]] = None


class TemplateVariable(BaseModel):
    name: str
    prompt: str = ""
    default: Any = ""
    kind: str = "text"  # text, confirm, select, password, int, float
    help: str = ""
    validation: Optional[VariableValidation] = None
    condition: Optional[str] = None  # Jinja2 expression, evaluated with current context
    choices: Optional[List[VariableChoice]] = None


class PostCommand(BaseModel):
    command: str
    description: str = ""
    condition: Optional[str] = None  # Optional condition, uses Jinja2 template syntax


class PresetValue(BaseModel):
    var_name: str
    value: Any


class TemplateConfig(BaseModel):
    name: str
    description: str = ""
    version: str = "1.0"
    variables: List[TemplateVariable] = Field(default_factory=list)
    post_commands: List[PostCommand] = Field(default_factory=list)

    @field_validator("variables")
    @classmethod
    def unique_variable_names(cls, v: List[TemplateVariable]) -> List[TemplateVariable]:
        names = [var.name for var in v]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ValueError(f"Duplicate variable names found: {duplicates}")
        return v


def load_template_config(config_path: str) -> TemplateConfig:
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Template config not found: {config_path}")

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if raw is None:
        raise ValueError(f"Empty config file: {config_path}")

    return TemplateConfig(**raw)


def discover_builtin_templates() -> List[Dict[str, str]]:
    import template_gen.templates as tmpl_pkg

    pkg_path = Path(tmpl_pkg.__path__[0])
    templates = []

    for entry in pkg_path.iterdir():
        if entry.is_dir():
            config_file = entry / "projgen.yaml"
            if config_file.exists():
                with open(config_file, "r", encoding="utf-8") as f:
                    raw = yaml.safe_load(f)
                templates.append({
                    "name": raw.get("name", entry.name),
                    "description": raw.get("description", ""),
                    "path": str(entry),
                    "config_file": str(config_file),
                })

    return templates


def discover_external_template(template_path: str) -> Optional[Dict[str, str]]:
    path = Path(template_path)
    config_file = path / "projgen.yaml"
    if not config_file.exists():
        return None
    with open(config_file, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return {
        "name": raw.get("name", path.name),
        "description": raw.get("description", ""),
        "path": str(path),
        "config_file": str(config_file),
    }