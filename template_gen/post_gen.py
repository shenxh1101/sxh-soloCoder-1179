"""Post-generation command executor with Jinja2 template support."""

import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

from jinja2 import Environment, BaseLoader

from template_gen.config import PostCommand


def execute_post_commands(
    commands: List[PostCommand],
    project_dir: str,
    context: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Execute post-generation commands in the generated project directory."""
    results = []
    env = Environment(loader=BaseLoader())
    project_path = Path(project_dir).resolve()

    for cmd_def in commands:
        if cmd_def.condition:
            try:
                tmpl = env.from_string("{{% if {} %}}1{{% else %}}0{{% endif %}}".format(cmd_def.condition))
                should_run = tmpl.render(**context).strip() == "1"
            except Exception:
                should_run = False
            if not should_run:
                continue

        rendered_cmd = env.from_string(cmd_def.command).render(**context)
        desc = cmd_def.description or rendered_cmd

        print(f"\n  \033[94mRunning: {desc}\033[0m")
        print(f"  \033[90m$ {rendered_cmd}\033[0m")

        try:
            proc = subprocess.run(
                rendered_cmd,
                shell=True,
                cwd=str(project_path),
                capture_output=True,
                text=True,
            )
            success = proc.returncode == 0
            results.append({
                "command": rendered_cmd,
                "description": desc,
                "success": success,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "returncode": proc.returncode,
            })

            if success:
                if proc.stdout.strip():
                    print(f"  \033[92m{proc.stdout.strip()}\033[0m")
            else:
                print(f"  \033[91mCommand failed (exit code {proc.returncode})\033[0m")
                if proc.stderr.strip():
                    print(f"  \033[91m{proc.stderr.strip()}\033[0m")

        except FileNotFoundError:
            results.append({
                "command": rendered_cmd,
                "description": desc,
                "success": False,
                "stdout": "",
                "stderr": "Command not found",
                "returncode": -1,
            })
            print(f"  \033[91mCommand not found: {rendered_cmd.split()[0]}\033[0m")

    return results