"""Skills in front of an agent: listed by name and description in its system prompt, read
in full with ``skills.load`` (see ``spec/skills.py`` for the files)."""

from __future__ import annotations

from ..spec.skills import FILE_CHARS, Skill
from ..tools.registry import Effect, Tool, tool


def skills_block(skills: list[Skill]) -> str:
    if not skills:
        return ""
    lines = [f"- {s.name}: {s.description}" for s in skills]
    return (
        "\n\n## Skills\nWhen a request matches one of these, read it first with"
        " skills.load(name) and follow it:\n" + "\n".join(lines)
    )


def skills_tool(skills: list[Skill]) -> Tool:
    by_name = {s.name: s for s in skills}

    @tool("skills.load", effect=Effect.READ)
    async def load(name: str, file: str | None = None) -> str:
        """Read one of the skills listed in your instructions (its full steps), or a file
        that skill mentions (``file``, relative to the skill)."""
        skill = by_name.get(name)
        if skill is None:
            raise ValueError(f"no skill {name!r}; skills: {sorted(by_name)}")
        if file is None:
            return skill.body()
        root = skill.folder.resolve()
        path = (root / file).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError(f"no file {file!r} in skill {name!r}")
        return path.read_text(encoding="utf-8", errors="replace")[:FILE_CHARS]

    return load
