"""Jinja2 template rendering engine with file/directory name templating."""

import os
from pathlib import Path
from typing import Any, Dict

from jinja2 import Environment, FileSystemLoader, TemplateNotFound


def render_project(
    template_dir: str,
    output_dir: str,
    context: Dict[str, Any],
    dry_run: bool = False,
) -> Dict[str, str]:
    """
    Render all files from template_dir to output_dir using Jinja2.
    Supports templating in file names, directory names, and file contents.
    Returns a mapping of {relative_path: rendered_content}.
    """
    template_dir = os.path.abspath(template_dir)
    rendered_files: Dict[str, str] = {}

    template_subdir = os.path.join(template_dir, "template")
    if not os.path.isdir(template_subdir):
        raise FileNotFoundError(
            f"Template subdirectory 'template/' not found in {template_dir}"
        )

    env = Environment(
        loader=FileSystemLoader(template_subdir),
        keep_trailing_newline=True,
    )

    for root, dirs, files in os.walk(template_subdir):
        rel_root = os.path.relpath(root, template_subdir)
        if rel_root == ".":
            rel_root = ""

        rendered_rel_root = _render_path(rel_root, context)

        for filename in files:
            template_rel_path = os.path.join(rel_root, filename).replace("\\", "/")
            rendered_filename = _render_path(filename, context)

            if filename.endswith((".jinja2", ".jinja", ".j2")):
                rendered_filename = _strip_template_ext(rendered_filename)

            content = _render_file_content(env, template_rel_path, context)

            if rendered_rel_root:
                relative_output = os.path.join(rendered_rel_root, rendered_filename).replace("\\", "/")
            else:
                relative_output = rendered_filename

            rendered_files[relative_output] = content

            if not dry_run:
                target_root = os.path.join(os.path.abspath(output_dir), rendered_rel_root)
                os.makedirs(target_root, exist_ok=True)
                output_path = os.path.join(target_root, rendered_filename)
                with open(output_path, "w", encoding="utf-8") as f:
                    f.write(content)

    return rendered_files


def _render_path(path_part: str, context: Dict[str, Any]) -> str:
    if not path_part:
        return path_part
    env = Environment(loader=FileSystemLoader("."))
    try:
        template = env.from_string(path_part)
        return template.render(**context)
    except Exception:
        return path_part


def _strip_template_ext(filename: str) -> str:
    for ext in (".jinja2", ".jinja", ".j2"):
        if filename.endswith(ext):
            return filename[: -len(ext)]
    return filename


def _render_file_content(
    env: Environment, template_path: str, context: Dict[str, Any]
) -> str:
    try:
        template = env.get_template(template_path)
        return template.render(**context)
    except TemplateNotFound:
        return ""
    except Exception:
        import traceback
        return f"[ERROR rendering {template_path}]\n{traceback.format_exc()}"