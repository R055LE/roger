"""Pydantic argument models and the tool registry.

Each tool is one ``ToolSpec``: a name, a description, a pydantic args model (``extra="forbid"`` so
the model can never smuggle unexpected fields), and whether it needs interactive confirmation.
``openai_tools`` renders the registry into the function-calling schema the LLM sees.
"""

from __future__ import annotations

from collections.abc import Callable
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


class ChannelGrant(ToolArgs):
    role: str  # role name/id or user id, resolved live (prefer read_only/private for @everyone)
    allow: list[PermName] = Field(min_length=1)  # permissions to allow this target at creation


class CreateChannelArgs(ToolArgs):
    name: str
    kind: Literal["text", "voice", "category"]
    category: str | None = None  # name or id; resolved live; invalid for kind=category
    topic: str | None = None  # text channels only
    read_only: bool = False  # text: deny send_messages for @everyone at creation (no confirm, §2.8)
    private: bool = False  # text/voice: deny @everyone view_channel — hidden channel (confirmed)
    grants: list[ChannelGrant] = Field(default_factory=list, max_length=10)  # per-role allow bits


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


class EditChannelArgs(ToolArgs):
    channel: str  # existing channel: name or id
    name: str | None = None  # new name (optional)
    topic: str | None = None  # new topic — text channels only (optional)
    category: str | None = None  # move under this category: name or id (optional)

    @model_validator(mode="after")
    def _at_least_one_change(self) -> EditChannelArgs:
        if self.name is None and self.topic is None and self.category is None:
            raise ValueError("specify at least one of: name, topic, category")
        return self


class PostMessageArgs(ToolArgs):
    channel: str  # target text channel: name or id
    content: str = Field(min_length=1, max_length=2000)  # body; mass mentions always suppressed


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
    # Optional per-call override: gate confirmation on the validated args (e.g. only when private).
    confirm_when: Callable[[Any], bool] | None = None

    def needs_confirm(self, args: Any) -> bool:
        if self.confirm_when is not None:
            return self.confirm_when(args)
        return self.requires_confirm


REGISTRY: dict[str, ToolSpec] = {
    "list_structure": ToolSpec(
        name="list_structure",
        description="Return the server's categories, channels, and roles. Read-only.",
        args_model=ListStructureArgs,
    ),
    "create_channel": ToolSpec(
        name="create_channel",
        description=(
            "Create a text, voice, or category channel. Optionally nest a text/voice channel "
            "under a category, set a text topic, and set access at creation — for ANY type, "
            "categories included: read_only (deny @everyone send; text only), private (hide from "
            "@everyone — use this for an admin-only category, whose child channels inherit it), "
            "and grants (per-role allow, e.g. let 'Admins' view a private category). Anything "
            "private requires owner confirmation; read_only and grants apply immediately."
        ),
        args_model=CreateChannelArgs,
        confirm_when=lambda args: args.private,
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
            "Set channel permission overwrites on a text, voice, or category channel. Target a "
            "role, member, @everyone, or 'self' (which means Roger itself). If a change hides a "
            "channel from @everyone, Roger automatically keeps its own access. The owner must "
            "confirm the exact change before it is applied."
        ),
        args_model=SetPermissionsArgs,
        requires_confirm=True,
    ),
    "edit_channel": ToolSpec(
        name="edit_channel",
        description=(
            "Rename an existing channel, change a text channel's topic, and/or move a channel "
            "into a category. Metadata only — it cannot delete a channel and cannot change "
            "permissions (use set_permissions for those). The owner must confirm the change first."
        ),
        args_model=EditChannelArgs,
        requires_confirm=True,
    ),
    "post_message": ToolSpec(
        name="post_message",
        description=(
            "Post a message as Roger into a text channel. Mass mentions (@everyone, @here, and "
            "role pings) are always suppressed. The owner must confirm the exact channel and "
            "text before it is sent."
        ),
        args_model=PostMessageArgs,
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
