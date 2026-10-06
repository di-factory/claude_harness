"""Skills in packs: procedures an agent loads when it needs them, not in every prompt
(the files; ``runtime/skills.py`` puts them in front of agents).

A pack lists skill folders in ``skills`` (paths relative to the pack). Each folder holds a
``SKILL.md`` whose frontmatter names it and says when to use it::

    ---
    name: refund-request
    description: How to handle a customer asking for a refund (checks, wording, limits).
    ---
    1. Ask for the order number...

A listed folder without a ``SKILL.md`` of its own is a folder of skills (each subfolder one).
Every agent's system prompt lists the skills by name and description only; the agent reads
one with ``skills.load(name)`` (and a file next to it with ``skills.load(name, file)``).
Skills are part of the signed solution, like prompts: trusted text, read-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

SKILL_FILE = "SKILL.md"
NAME_CHARS = 64
DESCRIPTION_CHARS = 1024
FILE_CHARS = 60_000


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    folder: Path

    def body(self) -> str:
        return _split((self.folder / SKILL_FILE).read_text(encoding="utf-8"))[1].strip()


def _split(text: str) -> tuple[dict[str, str], str]:
    """Frontmatter (simple ``key: value`` lines) and the body."""
    if not text.startswith("---"):
        return {}, text
    head, sep, body = text[3:].partition("\n---")
    if not sep:
        return {}, text
    meta: dict[str, str] = {}
    for line in head.splitlines():
        key, colon, value = line.partition(":")
        if colon and key.strip():
            meta[key.strip()] = value.strip().strip("\"'")
    return meta, body.partition("\n")[2]


def _folders(ref: str) -> list[Path]:
    root = Path(ref)
    if (root / SKILL_FILE).is_file():
        return [root]
    if root.is_dir():
        return sorted(p for p in root.iterdir() if (p / SKILL_FILE).is_file())
    return []


def discover(refs: list[str]) -> tuple[list[Skill], list[tuple[str, str, str]]]:
    """The skills under ``refs`` and what is wrong with them as ``(code, path, message)``:
    ``missing_file`` when a folder has no skill at all, ``invalid_skill`` otherwise."""
    skills: list[Skill] = []
    problems: list[tuple[str, str, str]] = []
    seen: dict[str, Path] = {}
    for index, ref in enumerate(refs):
        folders = _folders(ref)
        if not folders:
            problems.append(("missing_file", f"skills[{index}]", f"no {SKILL_FILE} in {ref}"))
        for folder in folders:
            meta, body = _split((folder / SKILL_FILE).read_text(encoding="utf-8"))
            name, description = meta.get("name", ""), meta.get("description", "")
            where = f"skills[{index}] ({folder.name})"
            if not name or not description:
                problems.append(
                    (
                        "invalid_skill",
                        where,
                        f"{SKILL_FILE} needs a frontmatter name and description",
                    )
                )
                continue
            if len(name) > NAME_CHARS or not all(c.isalnum() or c in "-_" for c in name):
                problems.append(
                    ("invalid_skill", where, f"skill name {name!r}: letters, digits, - and _ only")
                )
                continue
            if len(description) > DESCRIPTION_CHARS:
                problems.append(
                    ("invalid_skill", where, f"description over {DESCRIPTION_CHARS} characters")
                )
                continue
            if not body.strip():
                problems.append(
                    (
                        "invalid_skill",
                        where,
                        f"{SKILL_FILE} has no instructions after the frontmatter",
                    )
                )
                continue
            if name in seen:
                problems.append(("invalid_skill", where, f"skill {name!r} is also in {seen[name]}"))
                continue
            seen[name] = folder
            skills.append(Skill(name, description, folder))
    return skills, problems
