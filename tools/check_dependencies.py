from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _normalize_requirement(value: str) -> str:
    compact = re.sub(r"\s+", "", str(value or "").strip())
    match = re.match(r"([A-Za-z0-9_.-]+)(.*)$", compact)
    if not match:
        raise ValueError(f"Invalid requirement: {value!r}")
    name = match.group(1).replace("_", "-").replace(".", "-").casefold()
    return name + match.group(2)


def requirements_txt(path: Path) -> list[str]:
    output: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        output.append(_normalize_requirement(line))
    return sorted(output)


def pyproject_dependencies(path: Path) -> list[str]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    project = data.get("project") if isinstance(data, dict) else None
    values = project.get("dependencies") if isinstance(project, dict) else None
    if not isinstance(values, list):
        raise ValueError("pyproject.toml project.dependencies must be a list")
    return sorted(_normalize_requirement(str(value)) for value in values)


def main() -> int:
    requirements = requirements_txt(ROOT / "requirements.txt")
    project = pyproject_dependencies(ROOT / "pyproject.toml")
    if requirements != project:
        print("Dependency metadata drift detected.")
        print("requirements.txt:")
        for value in requirements:
            print(f"  {value}")
        print("pyproject.toml:")
        for value in project:
            print(f"  {value}")
        return 1
    print(f"Dependency metadata agrees for {len(requirements)} runtime dependencies.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
