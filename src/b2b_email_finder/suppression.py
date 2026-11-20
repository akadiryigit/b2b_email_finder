"""The never-contact list: emails or whole domains that must never be output again.

One entry per line; lines starting with # are comments. Both the CLI and the UI use
results/suppress.txt by default, and the UI can add entries to it.
"""

from __future__ import annotations

from pathlib import Path

DEFAULT_FILE = "suppress.txt"


def load(path: Path | None) -> set[str]:
    if path is None or not path.exists():
        return set()
    lines = path.read_text(encoding="utf-8").splitlines()
    return {line.strip().lower() for line in lines if line.strip() and not line.lstrip().startswith("#")}


def add(path: Path, entries: list[str]) -> list[str]:
    """Append new entries (emails or domains); returns the ones actually added."""
    existing = load(path)
    new = []
    for entry in entries:
        value = entry.strip().lower().removeprefix("mailto:")
        if value and value not in existing and value not in new and ("." in value):
            new.append(value)
    if new:
        path.parent.mkdir(parents=True, exist_ok=True)
        header = "" if path.exists() else "# Emails or domains that must never be contacted / output.\n"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(header + "".join(f"{value}\n" for value in new))
    return new


def is_suppressed(email: str, entries: set[str]) -> bool:
    email = email.lower()
    return email in entries or email.rpartition("@")[2] in entries
