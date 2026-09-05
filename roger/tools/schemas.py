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


class AuditPermissionsArgs(ToolArgs):
    """No arguments — audits Roger's configured delivery destinations."""


class ChannelGrant(ToolArgs):
    # @everyone and Roger's integration role are reserved for private/read_only invariants.
    role: str  # role name/id or user id, resolved live
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


class EditRoleArgs(ToolArgs):
    role: str  # existing role: name or id
    name: str | None = None  # new name (optional)
    color: str | None = None  # new hex "#RRGGBB" (optional)
    hoist: bool | None = None  # show separately in the member list (optional)
    mentionable: bool | None = None  # anyone can @mention it (optional)

    @model_validator(mode="after")
    def _at_least_one_change(self) -> EditRoleArgs:
        if all(v is None for v in (self.name, self.color, self.hoist, self.mentionable)):
            raise ValueError("specify at least one of: name, color, hoist, mentionable")
        return self


class DeleteRoleArgs(ToolArgs):
    role: str  # existing role: name or id


class AddMemberRoleArgs(ToolArgs):
    member: str  # a member: numeric Discord user id only (no name lookup — see tool description)
    role: str  # existing role: name or id


class RemoveMemberRoleArgs(ToolArgs):
    member: str  # a member: numeric Discord user id only
    role: str  # existing role: name or id


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


class ListForumPostsArgs(ToolArgs):
    forum: str  # existing forum channel: name or id
    include_archived: bool = False
    limit: int = Field(default=20, ge=1, le=50)


class CreateForumPostArgs(ToolArgs):
    forum: str  # existing forum channel: name or id
    title: str = Field(min_length=1, max_length=100)  # thread name; Discord's own cap
    content: str = Field(min_length=1, max_length=2000)  # starter message; forums require one
    tags: list[str] = Field(default_factory=list, max_length=5)  # names from the forum's tag set


class ReplyToForumPostArgs(ToolArgs):
    forum: str  # the post's parent forum: name or id
    post: str  # existing post (thread) in that forum: title or id
    content: str = Field(min_length=1, max_length=2000)


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


class RunSparkArgs(ToolArgs):
    """No arguments — triggers the spark job immediately."""


class ListFeedsArgs(ToolArgs):
    """No arguments — returns the digest's current feed list."""


class SuggestFeedsArgs(ToolArgs):
    urls: list[str] = Field(min_length=1, max_length=8)  # candidate feed URLs to vet


class AddFeedArgs(ToolArgs):
    url: str  # RSS/Atom feed URL; validated live before it is stored


class RemoveFeedArgs(ToolArgs):
    url: str  # exact stored URL (from list_feeds)


class ListPersonalFeedsArgs(ToolArgs):
    """No arguments — returns the owner's personal digest feed list."""


class SuggestPersonalFeedsArgs(ToolArgs):
    urls: list[str] = Field(min_length=1, max_length=8)  # candidate feed URLs to vet


class AddPersonalFeedArgs(ToolArgs):
    url: str  # RSS/Atom feed URL; validated live before it is stored


class RemovePersonalFeedArgs(ToolArgs):
    url: str  # exact stored URL (from list_personal_feeds)


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


class RemoveReactionArgs(ToolArgs):
    message: str  # message link or a bare message id
    emoji: str  # the exact emoji Roger reacted with
    channel: str | None = None  # channel name/id — required only when `message` is a bare id


class ListRoleMembersArgs(ToolArgs):
    role: str  # existing role: name or id


class ListAuditLogArgs(ToolArgs):
    limit: int = Field(default=20, ge=1, le=50)  # most recent N entries


class ListInvitesArgs(ToolArgs):
    """No arguments — returns safe metadata for all active invites."""


class ListWebhooksArgs(ToolArgs):
    """No arguments — returns all webhooks in the server."""


class ListScheduledEventsArgs(ToolArgs):
    """No arguments — returns all scheduled events."""


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
            "and grants (per-role allow, excluding @everyone and Roger's own role; e.g. let "
            "'Admins' view a private category). Anything "
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
    "edit_role": ToolSpec(
        name="edit_role",
        description=(
            "Rename an existing role, and/or change its color, hoist (show separately in the "
            "member list), or mentionable flag. Metadata only — it cannot touch permissions "
            "(roles are always zero-permission) and cannot target @everyone or a Discord-managed "
            "role (bot/integration/booster). The owner must confirm the change first."
        ),
        args_model=EditRoleArgs,
        requires_confirm=True,
    ),
    "delete_role": ToolSpec(
        name="delete_role",
        description=(
            "Permanently delete a role. Refuses @everyone and Discord-managed roles "
            "(bot/integration/booster). Roger cannot see who currently holds a role (no Members "
            "intent, §2.1) so this does not check membership — the owner must confirm the exact "
            "role first, and deletion cannot be undone."
        ),
        args_model=DeleteRoleArgs,
        requires_confirm=True,
    ),
    "add_member_role": ToolSpec(
        name="add_member_role",
        description=(
            "Give a member a role. `member` must be their numeric Discord user id (right-click "
            "→ Copy User ID) — Roger can't resolve a member by name without the Members intent. "
            "Refuses @everyone and Discord-managed roles. The confirm dialog shows the role's "
            "actual permissions, since an existing role (unlike one Roger creates) may not be "
            "zero-permission — the owner decides with that in view. Fails clearly if the role "
            "sits above Roger's own role in the hierarchy."
        ),
        args_model=AddMemberRoleArgs,
        requires_confirm=True,
    ),
    "remove_member_role": ToolSpec(
        name="remove_member_role",
        description=(
            "Remove a role from a member. `member` must be their numeric Discord user id. "
            "Refuses @everyone and Discord-managed roles. Fails clearly if the role sits above "
            "Roger's own role in the hierarchy."
        ),
        args_model=RemoveMemberRoleArgs,
        requires_confirm=True,
    ),
    "set_permissions": ToolSpec(
        name="set_permissions",
        description=(
            "Set channel permission overwrites on a text, voice, or category channel. Target a "
            "role, member, @everyone, or 'self' (which means Roger itself). Each target is a "
            "complete replacement: omitted allow/deny bits are cleared (an empty overwrite "
            "removes it). The confirmation preview shows live before/after state and every "
            "cleared bit. If a change hides a channel from @everyone, Roger automatically keeps "
            "its required access through its dedicated bot role. The owner must confirm the "
            "exact change before it is applied."
        ),
        args_model=SetPermissionsArgs,
        requires_confirm=True,
    ),
    "audit_permissions": ToolSpec(
        name="audit_permissions",
        description=(
            "Audit Roger's configured delivery destinations. Reports required and effective "
            "capabilities, missing capabilities and their overwrite layer, category sync state, "
            "dedicated bot-role identification, and a read-only role-based remediation plan. "
            "It never changes Discord. Run this before proposing a permission change."
        ),
        args_model=AuditPermissionsArgs,
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
    "list_forum_posts": ToolSpec(
        name="list_forum_posts",
        description=(
            "List posts (threads) in a forum channel: title, applied tags, reply count, and "
            "archived state. Active posts only unless include_archived is set. Read-only."
        ),
        args_model=ListForumPostsArgs,
    ),
    "create_forum_post": ToolSpec(
        name="create_forum_post",
        description=(
            "Start a new post in a forum channel: a title, a starter message, and optionally "
            "tags from the forum's own tag set. Mass mentions are always suppressed. The owner "
            "must confirm the exact forum, title, and text before it is sent."
        ),
        args_model=CreateForumPostArgs,
        requires_confirm=True,
    ),
    "reply_to_forum_post": ToolSpec(
        name="reply_to_forum_post",
        description=(
            "Post a reply as Roger into an existing forum post (thread). Mass mentions are "
            "always suppressed. The owner must confirm the exact post and text before it is sent."
        ),
        args_model=ReplyToForumPostArgs,
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
    "run_spark": ToolSpec(
        name="run_spark",
        description=(
            "Trigger the spark job (spotlight one feed item + a discussion question) "
            "immediately."
        ),
        args_model=RunSparkArgs,
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
    "list_personal_feeds": ToolSpec(
        name="list_personal_feeds",
        description="List the RSS/Atom feeds in the owner's personal digest (DM'd privately, "
        "separate from the public digest). Read-only.",
        args_model=ListPersonalFeedsArgs,
    ),
    "suggest_personal_feeds": ToolSpec(
        name="suggest_personal_feeds",
        description=(
            "Validate candidate RSS/Atom feed URLs WITHOUT adding them to the personal digest. "
            "Returns, per URL, whether it's a live feed, its title, and how many items it has. "
            "Use this to vet feeds you propose before calling add_personal_feed."
        ),
        args_model=SuggestPersonalFeedsArgs,
    ),
    "add_personal_feed": ToolSpec(
        name="add_personal_feed",
        description=(
            "Validate and add one RSS/Atom feed to the owner's personal digest. Fails if the URL "
            "isn't a live feed. Idempotent — adding an existing feed is a no-op."
        ),
        args_model=AddPersonalFeedArgs,
    ),
    "remove_personal_feed": ToolSpec(
        name="remove_personal_feed",
        description=(
            "Remove a feed from the owner's personal digest by its exact URL. Call "
            "list_personal_feeds first to get the exact URL."
        ),
        args_model=RemovePersonalFeedArgs,
    ),
    "set_presence": ToolSpec(
        name="set_presence",
        description=(
            "Set your own presence: status (online, idle, dnd, invisible) and/or an activity line "
            "— 'playing', 'watching', 'listening', or 'competing' plus text, or 'none' to clear "
            "it. Only the fields you pass change; the rest are kept. It's persisted, so it "
            "survives restarts. Discord shows the verb only in the profile popout — the member "
            "list shows your text alone — so pick text that reads naturally both after the verb "
            "and on its own (prefer 'the server logs' over 'over the server'). Cosmetic and "
            "self-only — no confirmation needed."
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
    "remove_reaction": ToolSpec(
        name="remove_reaction",
        description=(
            "Remove Roger's own reaction from a message (never another user's). Same message "
            "identification as add_reaction. Reversible — no confirmation needed."
        ),
        args_model=RemoveReactionArgs,
    ),
    "list_role_members": ToolSpec(
        name="list_role_members",
        description=(
            "List the members currently holding a role. Read-only. Falls back to 'unavailable' "
            "if the Members intent isn't enabled for the application (§2.1, ADR-0008) — same "
            "on-demand lookup delete_role's confirm preview uses, no standing member cache."
        ),
        args_model=ListRoleMembersArgs,
    ),
    "list_audit_log": ToolSpec(
        name="list_audit_log",
        description=(
            "Return Discord's own audit log: the most recent N entries (default 20, max 50), "
            "each with the action, who did it, the target, the reason (if given), and when. "
            "Read-only. Requires the 'View Audit Log' permission on Roger's role."
        ),
        args_model=ListAuditLogArgs,
    ),
    "list_invites": ToolSpec(
        name="list_invites",
        description=(
            "List safe metadata for all active invites: target channel, inviter, use count, and "
            "expiry. Invite codes and URLs are omitted so they never reach the model. Read-only. "
            "Requires the 'Manage Guild' permission on Roger's role."
        ),
        args_model=ListInvitesArgs,
    ),
    "list_webhooks": ToolSpec(
        name="list_webhooks",
        description=(
            "List webhooks configured in the server and which channel each posts to. "
            "Read-only. Requires the 'Manage Webhooks' permission on Roger's role."
        ),
        args_model=ListWebhooksArgs,
    ),
    "list_scheduled_events": ToolSpec(
        name="list_scheduled_events",
        description=(
            "List the server's scheduled events: name, start time, location, and status. "
            "Read-only, no extra permission needed."
        ),
        args_model=ListScheduledEventsArgs,
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
