"""Pydantic argument models and the tool registry.

Each tool is one ``ToolSpec``: a name, a description, a pydantic args model (``extra="forbid"`` so
the model can never smuggle unexpected fields), and whether it needs interactive confirmation.
``openai_tools`` renders the registry into the function-calling schema the LLM sees.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# The only permissions expressible through set_permissions. Anything outside this allowlist is
# unrepresentable — the model literally cannot ask for it (§7).
PermName = Literal[
    "view_channel",
    "send_messages",
    "read_message_history",
    "add_reactions",
    "embed_links",
    "attach_files",
    "connect",
    "speak",
    "create_public_threads",
    "send_messages_in_threads",
]


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListStructureArgs(ToolArgs):
    """No arguments — returns the full server structure."""


class CreateChannelArgs(ToolArgs):
    name: str
    kind: Literal["text", "voice", "category"]
    category: str | None = None  # name or id; resolved live; invalid for kind=category
    topic: str | None = None  # text channels only
    read_only: bool = False  # text: deny send_messages for @everyone at creation (no confirm, §2.8)


class CreateRoleArgs(ToolArgs):
    name: str
    color: str | None = None  # hex "#RRGGBB"
    hoist: bool = False
    mentionable: bool = False
    # permissions are intentionally NOT a parameter — the executor always passes Permissions.none()


class Overwrite(ToolArgs):
    target: str  # role name/id or user id; "@everyone" allowed (channel-scoped)
    allow: list[PermName] = Field(default_factory=list)
    deny: list[PermName] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_overlap(self) -> Overwrite:
        overlap = set(self.allow) & set(self.deny)
        if overlap:
            raise ValueError(f"permissions in both allow and deny: {sorted(overlap)}")
        return self


class SetPermissionsArgs(ToolArgs):
    channel: str  # name or id, resolved against live guild
    overwrites: list[Overwrite] = Field(min_length=1, max_length=10)


class RunDigestArgs(ToolArgs):
    """No arguments — triggers the digest job immediately."""


class ListFeedsArgs(ToolArgs):
    """No arguments — returns the digest's current feed list."""


class SuggestFeedsArgs(ToolArgs):
    urls: list[str] = Field(min_length=1, max_length=8)  # candidate feed URLs to vet


class AddFeedArgs(ToolArgs):
    url: str  # RSS/Atom feed URL; validated live before it is stored


class RemoveFeedArgs(ToolArgs):
    url: str  # exact stored URL (from list_feeds)


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
    "create_channel": ToolSpec(
        name="create_channel",
        description=(
            "Create a text, voice, or category channel. Optionally place a text/voice channel "
            "under an existing category, and make a text channel read-only for @everyone."
        ),
        args_model=CreateChannelArgs,
    ),
    "create_role": ToolSpec(
        name="create_role",
        description=(
            "Create a cosmetic role. It is always created with zero permissions; grant "
            "access via channel overwrites, never role permissions."
        ),
        args_model=CreateRoleArgs,
    ),
    "set_permissions": ToolSpec(
        name="set_permissions",
        description=(
            "Set channel-scoped permission overwrites on an existing channel. The owner must "
            "confirm the exact change before it is applied."
        ),
        args_model=SetPermissionsArgs,
        requires_confirm=True,
    ),
    "run_digest": ToolSpec(
        name="run_digest",
        description="Trigger the RSS/Atom digest job immediately.",
        args_model=RunDigestArgs,
    ),
    "list_feeds": ToolSpec(
        name="list_feeds",
        description="List the RSS/Atom feeds currently in the daily digest. Read-only.",
        args_model=ListFeedsArgs,
    ),
    "suggest_feeds": ToolSpec(
        name="suggest_feeds",
        description=(
            "Validate candidate RSS/Atom feed URLs WITHOUT adding them. Returns, per URL, "
            "whether it's a live feed, its title, and how many items it has. Use this to vet "
            "feeds you propose before calling add_feed."
        ),
        args_model=SuggestFeedsArgs,
    ),
    "add_feed": ToolSpec(
        name="add_feed",
        description=(
            "Validate and add one RSS/Atom feed to the daily digest. Fails if the URL isn't a "
            "live feed. Idempotent — adding an existing feed is a no-op."
        ),
        args_model=AddFeedArgs,
    ),
    "remove_feed": ToolSpec(
        name="remove_feed",
        description=(
            "Remove a feed from the daily digest by its exact URL. Call list_feeds first to get "
            "the exact URL."
        ),
        args_model=RemoveFeedArgs,
    ),
}


def openai_tools(names: list[str]) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for name in names:
        spec = REGISTRY[name]
        schema = spec.args_model.model_json_schema()
        parameters: dict[str, Any] = {
            "type": "object",
            "properties": schema.get("properties", {}),
            "required": schema.get("required", []),
            "additionalProperties": False,
        }
        if "$defs" in schema:  # nested models (e.g. Overwrite) referenced via $ref
            parameters["$defs"] = schema["$defs"]
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": parameters,
                },
            }
        )
    return tools
