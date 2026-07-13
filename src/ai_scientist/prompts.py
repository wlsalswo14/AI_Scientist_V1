from __future__ import annotations

from importlib.resources import files


def load_prompt(name: str) -> str:
    path = files("ai_scientist").joinpath("prompt_text", f"{name}.md")
    return path.read_text(encoding="utf-8")

