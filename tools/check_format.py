from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".py", ".md", ".txt", ".toml", ".json", ".yml", ".yaml", ".css", ".js", ".html", ".sh", ".bat"}
EXCLUDED = {".git", ".venv", "venv", "dist", "build", "__pycache__"}


def main() -> int:
    problems: list[str] = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.suffix.casefold() not in TEXT_SUFFIXES:
            continue
        if any(part in EXCLUDED for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        relative = path.relative_to(ROOT)
        if "\r" in text:
            problems.append(f"{relative}: CRLF/CR character")
        if text and not text.endswith("\n"):
            problems.append(f"{relative}: missing final newline")
        for number, line in enumerate(text.splitlines(), 1):
            if line.rstrip(" \t") != line:
                problems.append(f"{relative}:{number}: trailing whitespace")
            if "\t" in line and path.suffix in {".py", ".yml", ".yaml"}:
                problems.append(f"{relative}:{number}: tab indentation")
    if problems:
        print("Formatting checks failed:")
        print("\n".join(problems))
        return 1
    print("Formatting checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
