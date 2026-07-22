"""Pydantic argument models and the tool registry.

Each tool is one ``ToolSpec``: a name, a description, a pydantic args model (``extra="forbid"`` so
the model can never smuggle unexpected fields), and whether it needs interactive confirmation.
``openai_tools`` renders the registry into the function-calling schema the LLM sees.

P2 ships only ``list_structure`` (read-only). create/role/permission tools arrive in P3.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListStructureArgs(ToolArgs):
    """No arguments — returns the full server structure."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[ToolArgs]
    requires_confirm: bool = False


REGISTRY: dict[str, ToolSpec] = {
    "list_structure": ToolSpec(
        name="list_structure",
        description="Return the server's categories, channels, and roles. Read-only.",
        args_model=ListStructureArgs,
    ),
}


def openai_tools(names: list[str]) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for name in names:
        spec = REGISTRY[name]
        schema = spec.args_model.model_json_schema()
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": {
                        "type": "object",
                        "properties": schema.get("properties", {}),
                        "required": schema.get("required", []),
                        "additionalProperties": False,
                    },
                },
            }
        )
    return tools
