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


class MoveChannelArgs(ToolArgs):
    channel: str  # channel or category to reorder: name or id
    position: Literal["top", "bottom"] | None = None  # move to the top/bottom of its group
    before: str | None = None  # place directly above this sibling: name or id
    after: str | None = None  # place directly below this sibling: name or id

    @model_validator(mode="after")
    def _exactly_one_anchor(self) -> MoveChannelArgs:
        anchors = [a for a in (self.position, self.before, self.after) if a is not None]
        if len(anchors) != 1:
            raise ValueError("specify exactly one of: position, before, after")
        return self


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


# --------------------------------------------------------------------------- toys (self / read)

StatusName = Literal["online", "idle", "dnd", "invisible"]
# "none" clears the activity line; the four verbs render as "Playing/Watching/Listening to/
# Competing in <text>". Custom statuses are omitted — they don't render reliably for bots.
ActivityKind = Literal["playing", "watching", "listening", "competing", "none"]
_ACTIVITY_VERBS = {"playing", "watching", "listening", "competing"}


class SetPresenceArgs(ToolArgs):
    # All three are optional and merge over the stored presence — passing only `status` keeps the
    # current activity, and vice versa. At least one must be given.
    status: StatusName | None = None
    activity: ActivityKind | None = None  # a verb, or "none" to clear the line
    text: str | None = Field(default=None, max_length=100)  # the activity line; needed with a verb

    @model_validator(mode="after")
    def _coherent(self) -> SetPresenceArgs:
        if self.status is None and self.activity is None and self.text is None:
            raise ValueError("specify at least one of: status, activity, text")
        if self.activity in _ACTIVITY_VERBS and not (self.text and self.text.strip()):
            raise ValueError(f"activity {self.activity!r} needs non-empty text")
        if self.activity == "none" and self.text:
            raise ValueError("activity 'none' clears the line — don't also pass text")
        if self.text is not None and self.activity is None:
            raise ValueError("pass activity (a verb) together with text")
        return self


class SetNicknameArgs(ToolArgs):
    # Roger's own guild nickname. Empty string resets to the default name. Discord's cap is 32.
    nickname: str = Field(max_length=32)


class ServerStatsArgs(ToolArgs):
    """No arguments — returns a read-only snapshot of server stats."""


class AddReactionArgs(ToolArgs):
    message: str  # message link (right-click → Copy Message Link) or a bare message id
    emoji: str  # a standard emoji, or a custom server emoji as :name: or <:name:id>
    channel: str | None = None  # channel name/id — required only when `message` is a bare id


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
    "move_channel": ToolSpec(
        name="move_channel",
        description=(
            "Reorder a channel or category. Move it to the 'top' or 'bottom' of its group, or "
            "place it directly before/after a sibling — a category next to another category, a "
            "channel next to a channel in the same category. Position only: it never renames, "
            "moves a channel between categories (use edit_channel for that), or changes "
            "permissions. The owner must confirm the move first."
        ),
        args_model=MoveChannelArgs,
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
    "set_presence": ToolSpec(
        name="set_presence",
        description=(
            "Set your own presence: status (online, idle, dnd, invisible) and/or an activity line "
            "— 'playing', 'watching', 'listening', or 'competing' plus text, or 'none' to clear "
            "it. Only the fields you pass change; the rest are kept. It's persisted, so it "
            "survives restarts. Cosmetic and self-only — no confirmation needed."
        ),
        args_model=SetPresenceArgs,
    ),
    "set_nickname": ToolSpec(
        name="set_nickname",
        description=(
            "Set your own nickname in this server (max 32 characters; an empty string resets to "
            "the default name). Self-only and reversible — no confirmation needed."
        ),
        args_model=SetNicknameArgs,
    ),
    "server_stats": ToolSpec(
        name="server_stats",
        description=(
            "Return a read-only snapshot of the server: member count, channels by type, roles, "
            "custom emoji, boost tier, and how old the server is."
        ),
        args_model=ServerStatsArgs,
    ),
    "add_reaction": ToolSpec(
        name="add_reaction",
        description=(
            "React to a message with an emoji. Identify the message by its link (right-click → "
            "Copy Message Link) or by its id together with the channel. The emoji may be a "
            "standard emoji or a custom server emoji (:name: or <:name:id>). Reversible — no "
            "confirmation needed."
        ),
        args_model=AddReactionArgs,
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
