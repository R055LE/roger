"""A small bag of dependencies passed to executors that need more than the guild.

Kept dependency-free (``Any`` fields) so the tools package never imports the bot, llm, or store
modules — no import cycles. Only ``run_digest`` uses it today.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ToolContext:
    llm: Any = None
    store: Any = None
    settings: Any = None
    client: Any = None
