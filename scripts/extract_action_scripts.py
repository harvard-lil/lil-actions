#!/usr/bin/env python3
"""Extract the bash from composite action steps so shellcheck can see it.

The `run:` blocks in */action.yml are real bash, but they live inside YAML and
shellcheck cannot read them there. This writes each one to its own file in an
output directory, named <action>--step<N>-<step-id-or-name>.sh, so shellcheck's
own output identifies which step a finding belongs to.

GitHub expressions (${{ ... }}) are substituted for a shell variable reference
first. They are interpolated by the runner before bash ever sees them, so
leaving them in would only produce parse errors. A *variable* rather than a
literal matters: substituting a literal makes `[ "x" != "push" ]` look like a
constant comparison and shellcheck reports SC2050 on code that is fine.

Most actions here pass inputs via `env:` and contain no expressions at all; the
substitution exists for the ones that do.
"""

import argparse
import re
import sys
from pathlib import Path

import yaml

EXPRESSION = re.compile(r"\$\{\{.*?\}\}", re.DOTALL)
PLACEHOLDER = "$gha_expr"


def slug(text):
    return re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower() or "step"


def extract(action_path, out_dir):
    doc = yaml.safe_load(action_path.read_text())
    steps = ((doc or {}).get("runs") or {}).get("steps") or []
    written = []

    for index, step in enumerate(steps):
        script = step.get("run")
        # Only bash steps. A composite action may also use python, pwsh, etc.
        if not script or step.get("shell") not in ("bash", "sh"):
            continue

        name = slug(step.get("id") or step.get("name") or f"step{index}")
        out = out_dir / f"{action_path.parent.name}--{index}-{name}.sh"
        shebang = "#!/usr/bin/env bash\n" if step["shell"] == "bash" else "#!/bin/sh\n"
        body = EXPRESSION.sub(PLACEHOLDER, script)
        # Declare the placeholder so it doesn't read as an unassigned variable.
        preamble = 'gha_expr="${gha_expr:-}"\n' if PLACEHOLDER in body else ""
        out.write_text(shebang + preamble + body)
        written.append(out)

    return written


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)

    written = []
    for action_path in sorted(args.root.glob("*/action.yml")):
        written.extend(extract(action_path, args.out_dir))

    if not written:
        print("No bash steps found -- refusing to report success.", file=sys.stderr)
        return 1

    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
