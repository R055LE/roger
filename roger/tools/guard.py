"""Guard rules: sanitizers and business logic, kept pure so they can be unit-tested.

A ``GuardError`` is a *structured, model-visible* refusal (name collision, ambiguous target, bad
input) — the admin loop turns it into a tool result, never an exception the model can't see.
"""

from __future__ import annotations

import re

_CHANNEL_DISALLOWED = re.compile(r"[^a-z0-9\-_]")
_MULTI_DASH = re.compile(r"-{2,}")
_HEX_COLOR = re.compile(r"#?([0-9a-fA-F]{6})")


class GuardError(Exception):
    """A structured refusal surfaced back to the model as a tool result."""


def sanitize_channel_name(raw: str) -> str:
    """Discord-style text-channel slug: lowercase, spaces to dashes, ``[a-z0-9-_]`` only."""
    slug = raw.strip().lower().replace(" ", "-")
    slug = _CHANNEL_DISALLOWED.sub("", slug)
    slug = _MULTI_DASH.sub("-", slug).strip("-")
    if not 1 <= len(slug) <= 100:
        raise GuardError(f"can't make a valid channel name from {raw!r}")
    return slug


def sanitize_display_name(raw: str) -> str:
    """Category/role/voice display name: keep case, strip control chars, length 1-100."""
    cleaned = "".join(ch for ch in raw if ch.isprintable()).strip()
    if not 1 <= len(cleaned) <= 100:
        raise GuardError(f"invalid name: {raw!r}")
    return cleaned


def check_no_duplicate(kind: str, name: str, existing_names: list[str]) -> None:
    if name.casefold() in {n.casefold() for n in existing_names}:
        raise GuardError(f"a {kind} named {name!r} already exists")


def resolve_one(query: str, items: list[tuple[int, str]]) -> tuple[int, str]:
    """Resolve a name-or-id against ``(id, name)`` pairs (case-insensitive; ambiguity errors)."""
    q = str(query).strip()
    if q.isdigit():
        for item_id, item_name in items:
            if str(item_id) == q:
                return item_id, item_name
    matches = [(i, n) for i, n in items if n.casefold() == q.casefold()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise GuardError(f"{query!r} is ambiguous — use the id or a more specific name")
    raise GuardError(f"no match for {query!r}")


def parse_color(value: str) -> int:
    match = _HEX_COLOR.fullmatch(value.strip())
    if not match:
        raise GuardError(f"invalid color {value!r}, expected #RRGGBB")
    return int(match.group(1), 16)
