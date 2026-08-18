# Personal Digest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** a feed roundup DM'd to the owner only, on its own feed list and schedule, separate from
the existing public digest.

**Architecture:** every piece mirrors an existing Digest or Giga Brain mechanism exactly — a second
`personal_feeds` table (mirrors `feeds`), a second scheduled job sharing Digest's model/budget
(mirrors `run_digest_job`, with delivery copied from `run_gigabrain_suggestion`'s DM-or-channel
pattern), and four mirrored curation tools. No new abstractions.

**Tech Stack:** `aiosqlite`, `feedparser`, `discord.py` `tasks.loop`, `pydantic-settings` — all
already in use, nothing new.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-18-personal-digest-design.md` — this plan implements it
  exactly; where anything here seems to conflict, the spec governs and the discrepancy should be
  flagged, not silently resolved.
- No new model or budget config — the job shares `MODEL_DIGEST` / `DAILY_TOKENS_DIGEST` /
  `DAILY_USD_DIGEST` via `llm.complete("digest", messages)`.
- `personal_digest_channel_id` unset means DM the owner; set means post there instead — same
  fallback shape as `gigabrain_channel_id`.
- `PERSONAL_DIGEST_FEEDS` seeds `personal_feeds` once; after that the store is authoritative (same
  "seed once" rule as `DIGEST_FEEDS`).
- Feedparser/RSS only — no scraping, no new source types.
- Every new `Settings` field must be forwarded in `compose.yaml`'s `environment:` block in the same
  task that adds it — `tests/test_compose.py::test_every_setting_is_forwarded_by_compose` fails
  otherwise, and per ADR precedent (the compose-vars-fix during the dollar-budget-gate work) this is
  not optional cleanup, it's part of the task.
- Run `pytest -q` and `ruff check .` before every commit. Both must be clean.

---

### Task 1: Storage — the `personal_feeds` table

**Files:**
- Modify: `roger/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Produces: `Store.list_personal_feeds() -> list[dict]` (rows with `url`, `title`, `added_ts`,
  ordered by `added_ts, url`), `Store.add_personal_feed(url: str, title: str | None) -> bool`,
  `Store.remove_personal_feed(url: str) -> bool`, `Store.seed_personal_feeds(urls: list[str]) ->
  int`, `Store.count_personal_feeds() -> int`. Every later task that touches storage uses these
  exact names.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_store.py`, right after `test_feed_crud_and_dedupe` (around line 73):

```python
async def test_personal_feed_crud(tmp_path):
    store = await Store(str(tmp_path / "roger.db")).open()
    try:
        assert await store.count_personal_feeds() == 0
        assert await store.add_personal_feed("http://a", "A") is True
        assert await store.add_personal_feed("http://a", "A") is False  # duplicate URL ignored
        assert await store.add_personal_feed("http://b", None) is True
        assert [f["url"] for f in await store.list_personal_feeds()] == ["http://a", "http://b"]

        assert await store.remove_personal_feed("http://a") is True
        assert await store.remove_personal_feed("http://a") is False  # already gone
        assert [f["url"] for f in await store.list_personal_feeds()] == ["http://b"]
    finally:
        await store.close()


async def test_personal_feeds_are_isolated_from_the_public_list(tmp_path):
    store = await Store(str(tmp_path / "roger.db")).open()
    try:
        await store.add_feed("http://shared", None)
        await store.add_personal_feed("http://shared", None)
        assert await store.count_feeds() == 1
        assert await store.count_personal_feeds() == 1
        await store.remove_feed("http://shared")
        assert await store.count_feeds() == 0
        assert await store.count_personal_feeds() == 1  # independent lists
    finally:
        await store.close()
```

Add to `tests/test_store.py`, right after `test_seed_feeds_ignores_existing` (end of file):

```python


async def test_seed_personal_feeds_ignores_existing(tmp_path):
    store = await Store(str(tmp_path / "roger.db")).open()
    try:
        await store.add_personal_feed("http://a", None)
        await store.seed_personal_feeds(["http://a", "http://b"])  # "http://a" already present
        assert {f["url"] for f in await store.list_personal_feeds()} == {"http://a", "http://b"}
    finally:
        await store.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_store.py -k personal_feed -v`
Expected: FAIL — `AttributeError: 'Store' object has no attribute 'add_personal_feed'` (and
similarly for the other three new methods).

- [ ] **Step 3: Add the table and the five methods**

In `roger/store.py`, add to `_SCHEMA` right after the `feeds` table definition (after line 86):

```python
CREATE TABLE IF NOT EXISTS personal_feeds (
    url      TEXT PRIMARY KEY,
    title    TEXT,
    added_ts REAL NOT NULL
);
```

Add the five methods right after `seed_feeds` (after line 352, before the `# --- digest dedupe (§9)
---` comment):

```python
    # --- personal digest feed list (owner-only; seeded once from PERSONAL_DIGEST_FEEDS) ---

    async def list_personal_feeds(self) -> list[dict[str, Any]]:
        cursor = await self._conn.execute(
            "SELECT url, title, added_ts FROM personal_feeds ORDER BY added_ts, url"
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def count_personal_feeds(self) -> int:
        cursor = await self._conn.execute("SELECT COUNT(*) FROM personal_feeds")
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def add_personal_feed(self, url: str, title: str | None) -> bool:
        """Insert a personal feed. Returns True if newly added, False if the URL already existed."""
        cursor = await self._conn.execute(
            "INSERT OR IGNORE INTO personal_feeds (url, title, added_ts) VALUES (?, ?, ?)",
            (url, title, time.time()),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def remove_personal_feed(self, url: str) -> bool:
        """Delete a personal feed by exact URL. Returns True if a row was removed."""
        cursor = await self._conn.execute("DELETE FROM personal_feeds WHERE url = ?", (url,))
        await self._conn.commit()
        return cursor.rowcount > 0

    async def seed_personal_feeds(self, urls: list[str]) -> int:
        now = time.time()
        await self._conn.executemany(
            "INSERT OR IGNORE INTO personal_feeds (url, title, added_ts) VALUES (?, ?, ?)",
            [(url, None, now) for url in urls],
        )
        await self._conn.commit()
        return len(urls)
```

No migration is needed — `CREATE TABLE IF NOT EXISTS` handles a live DB that predates this table the
same way it already handles every other table.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_store.py -v`
Expected: PASS (all tests in the file, not just the new ones — confirms nothing else broke).

- [ ] **Step 5: Commit**

```bash
git add roger/store.py tests/test_store.py
git commit -m "feat: add personal_feeds table and CRUD methods"
```

---

### Task 2: Config — settings, env forwarding, and the derived property

**Files:**
- Modify: `roger/config.py`
- Modify: `roger.env.example`
- Modify: `compose.yaml`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `settings.personal_digest_feeds: str`, `settings.personal_digest_channel_id: int |
  None`, `settings.personal_digest_hour: int`, `settings.personal_feeds: list[str]` (property).
  Task 3 and Task 5 read these exact names.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config.py`, right after `test_empty_digest_channel_id_becomes_none` (after line
62):

```python


def test_personal_digest_defaults(monkeypatch):
    _set_required(monkeypatch)
    settings = Settings()
    assert settings.personal_digest_feeds == ""
    assert settings.personal_feeds == []
    assert settings.personal_digest_channel_id is None
    assert settings.personal_digest_hour == 7


def test_personal_feeds_is_parsed_to_list(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("PERSONAL_DIGEST_FEEDS", "http://a, http://b ,http://c")
    assert Settings().personal_feeds == ["http://a", "http://b", "http://c"]


def test_empty_personal_digest_channel_id_becomes_none(monkeypatch):
    _set_required(monkeypatch)
    monkeypatch.setenv("PERSONAL_DIGEST_CHANNEL_ID", "")
    assert Settings().personal_digest_channel_id is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_config.py -k personal -v`
Expected: FAIL — `pydantic_core._pydantic_core.ValidationError` or `AttributeError` (the field
doesn't exist yet).

- [ ] **Step 3: Add the settings, property, and validator entry**

In `roger/config.py`, add to the `# --- digest ---` block (after line 69, `digest_hour: int = 8`):

```python

    # --- personal digest (owner-only, DM by default) ---
    personal_digest_feeds: str = ""
    # unset = DM the owner directly; set = post there instead (same shape as digest_channel_id).
    personal_digest_channel_id: int | None = None
    personal_digest_hour: int = 7
```

Update the validator (line 84-86) to include the new field:

```python
    @field_validator(
        "digest_channel_id",
        "ops_channel_id",
        "gigabrain_channel_id",
        "personal_digest_channel_id",
        mode="before",
    )
```

Add the derived property right after `feeds` (after line 112):

```python

    @property
    def personal_feeds(self) -> list[str]:
        return _split_csv(self.personal_digest_feeds)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_config.py -v`
Expected: PASS.

- [ ] **Step 5: Forward the new settings in `roger.env.example` and `compose.yaml`**

In `roger.env.example`, add right after `DIGEST_HOUR=8` (line 67), before `TZ=America/Detroit`:

```
# --- personal digest ---
# comma-separated RSS/Atom URLs, curated separately from the public digest above. Seeds once, same
# rule as DIGEST_FEEDS.
PERSONAL_DIGEST_FEEDS=
# unset = DM the owner directly; set = post there instead (same shape as DIGEST_CHANNEL_ID)
PERSONAL_DIGEST_CHANNEL_ID=
# local hour 0-23
PERSONAL_DIGEST_HOUR=7
```

In `compose.yaml`, add right after `DIGEST_HOUR: ${DIGEST_HOUR:-8}` (line 45), before
`OPS_CHANNEL_ID`:

```yaml
      PERSONAL_DIGEST_FEEDS: ${PERSONAL_DIGEST_FEEDS:-}
      PERSONAL_DIGEST_CHANNEL_ID: ${PERSONAL_DIGEST_CHANNEL_ID:-}
      PERSONAL_DIGEST_HOUR: ${PERSONAL_DIGEST_HOUR:-7}
```

- [ ] **Step 6: Run the compose-forwarding gate and the full suite**

Run: `pytest tests/test_compose.py tests/test_config.py -v`
Expected: PASS — confirms the three new fields are forwarded and nothing dangles.

Run: `pytest -q`
Expected: PASS (full suite).

- [ ] **Step 7: Commit**

```bash
git add roger/config.py roger.env.example compose.yaml tests/test_config.py
git commit -m "feat: add PERSONAL_DIGEST_* settings"
```

---

### Task 3: Digest brain — the second job

**Files:**
- Modify: `roger/brains/digest.py`
- Test: `tests/test_digest.py`

**Interfaces:**
- Consumes: `Store.list_personal_feeds`, `Store.add_personal_feed` (Task 1);
  `settings.personal_feeds`, `settings.personal_digest_channel_id` (Task 2); the existing
  module-private `_collect_new(feeds, store)` and `_summarize(entries, llm)` (unchanged, reused
  as-is).
- Produces: `seed_personal_feeds_if_empty(store, settings) -> int`,
  `run_personal_digest_job(*, client, settings, llm, store) -> dict[str, Any]`. Task 5's scheduling
  loop calls both by these exact names.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_digest.py`, at the end of the file:

```python


# --------------------------------------------------------------------------- personal digest


class FakeDMChannel:
    def __init__(self):
        self.sent = []

    async def send(self, embed=None, content=None):
        self.sent.append(embed if embed is not None else content)


class FakeUser:
    def __init__(self, raise_on_create_dm=None):
        self._raise_on_create_dm = raise_on_create_dm
        self.dm_channel = FakeDMChannel()

    async def create_dm(self):
        if self._raise_on_create_dm is not None:
            raise self._raise_on_create_dm
        return self.dm_channel


class FakePersonalClient:
    def __init__(self, user=None, channel=None):
        self._user = user
        self._channel = channel

    def get_channel(self, channel_id):
        return self._channel

    async def fetch_user(self, user_id):
        return self._user


def _http_error(kind, status):
    """Build a real discord HTTP error without a live aiohttp response."""
    response = SimpleNamespace(status=status, reason="test")
    return kind(response, "boom")


def _personal_settings(channel_id=None, tz="America/Detroit", owner_id=1):
    return SimpleNamespace(personal_digest_channel_id=channel_id, tz=tz, owner_id=owner_id)


async def _personal_store(tmp_path, feeds=("http://pf",)):
    store = await Store(str(tmp_path / "pdig.db")).open()
    for url in feeds:
        await store.add_personal_feed(url, None)
    return store


async def test_personal_seed_if_empty_is_one_shot(tmp_path):
    store = await _personal_store(tmp_path, feeds=())  # start empty
    try:
        seeded = await digest.seed_personal_feeds_if_empty(
            store, SimpleNamespace(personal_feeds=["http://s1", "http://s2"])
        )
        assert seeded == 2
        assert await store.count_personal_feeds() == 2
        # A later env change does NOT re-seed once the store is populated.
        again = await digest.seed_personal_feeds_if_empty(
            store, SimpleNamespace(personal_feeds=["http://s3"])
        )
        assert again == 0
    finally:
        await store.close()


async def test_personal_not_configured_when_no_feeds(tmp_path):
    store = await _personal_store(tmp_path, feeds=())
    try:
        user = FakeUser()
        out = await digest.run_personal_digest_job(
            client=FakePersonalClient(user=user),
            settings=_personal_settings(),
            llm=FakeLLM([]),
            store=store,
        )
        assert "not configured" in out["status"]
        assert user.dm_channel.sent == []
    finally:
        await store.close()


async def test_personal_no_new_items_skips(tmp_path, monkeypatch):
    store = await _personal_store(tmp_path)
    try:
        monkeypatch.setattr(digest.feedparser, "parse", lambda url: _feed([]))
        out = await digest.run_personal_digest_job(
            client=FakePersonalClient(user=FakeUser()),
            settings=_personal_settings(),
            llm=FakeLLM([]),
            store=store,
        )
        assert out["status"] == "no new items"
    finally:
        await store.close()


async def test_personal_posts_via_dm_when_no_channel_configured(tmp_path, monkeypatch):
    store = await _personal_store(tmp_path)
    try:
        monkeypatch.setattr(digest.feedparser, "parse", lambda url: _feed([_entry("n1")]))
        user = FakeUser()
        out = await digest.run_personal_digest_job(
            client=FakePersonalClient(user=user),
            settings=_personal_settings(channel_id=None),
            llm=FakeLLM([_resp("summary")]),
            store=store,
        )
        assert out["status"] == "posted" and out["count"] == 1
        assert len(user.dm_channel.sent) == 1
        assert isinstance(user.dm_channel.sent[0], discord.Embed)

        out2 = await digest.run_personal_digest_job(
            client=FakePersonalClient(user=user),
            settings=_personal_settings(channel_id=None),
            llm=FakeLLM([]),
            store=store,
        )
        assert out2["status"] == "no new items"  # marked seen after the first post
    finally:
        await store.close()


async def test_personal_posts_to_channel_when_configured(tmp_path, monkeypatch):
    store = await _personal_store(tmp_path)
    try:
        monkeypatch.setattr(digest.feedparser, "parse", lambda url: _feed([_entry("n1")]))
        channel = FakeChannel()
        out = await digest.run_personal_digest_job(
            client=FakePersonalClient(user=FakeUser(), channel=channel),
            settings=_personal_settings(channel_id=99),
            llm=FakeLLM([_resp("summary")]),
            store=store,
        )
        assert out["status"] == "posted"
        assert len(channel.sent) == 1
    finally:
        await store.close()


async def test_personal_dm_creation_failure_is_reported(tmp_path, monkeypatch):
    store = await _personal_store(tmp_path)
    try:
        monkeypatch.setattr(digest.feedparser, "parse", lambda url: _feed([_entry("n1")]))
        user = FakeUser(raise_on_create_dm=_http_error(discord.Forbidden, 403))
        out = await digest.run_personal_digest_job(
            client=FakePersonalClient(user=user),
            settings=_personal_settings(channel_id=None),
            llm=FakeLLM([_resp("summary")]),
            store=store,
        )
        assert "DM failed" in out["status"]
    finally:
        await store.close()


async def test_personal_budget_skips_post_and_stays_retryable(tmp_path, monkeypatch):
    store = await _personal_store(tmp_path)
    try:
        monkeypatch.setattr(digest.feedparser, "parse", lambda url: _feed([_entry("n1")]))
        user = FakeUser()
        out = await digest.run_personal_digest_job(
            client=FakePersonalClient(user=user),
            settings=_personal_settings(channel_id=None),
            llm=FakeLLM([BudgetExceeded("digest", 100, 50)]),
            store=store,
        )
        assert "budget" in out["status"]
        assert user.dm_channel.sent == []
        assert len(await _collect_new(["http://pf"], store)) == 1  # not marked seen
    finally:
        await store.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_digest.py -k personal -v`
Expected: FAIL — `AttributeError: module 'roger.brains.digest' has no attribute
'seed_personal_feeds_if_empty'` (and similarly once that's added, for `run_personal_digest_job`).

- [ ] **Step 3: Implement the two functions**

In `roger/brains/digest.py`, add right after `seed_feeds_if_empty` (after line 85):

```python


async def seed_personal_feeds_if_empty(store: Store, settings: Any) -> int:
    """One-time bootstrap: import PERSONAL_DIGEST_FEEDS the first time the table is empty.

    Same one-shot rule as ``seed_feeds_if_empty`` — after the initial seed the store is
    authoritative.
    """
    if await store.count_personal_feeds() > 0:
        return 0
    return await store.seed_personal_feeds(settings.personal_feeds)


async def run_personal_digest_job(
    *, client: Any, settings: Any, llm: LLM, store: Store
) -> dict[str, Any]:
    """Like ``run_digest_job``, but sourced from the personal feed list and delivered privately.

    Delivery copies ``run_gigabrain_suggestion``'s DM-or-channel pattern: the configured channel
    if set, else a DM to the owner. Unlike the public digest, no channel is required to be
    "configured" — "not configured" here means "no feeds," since a DM destination is always
    reachable in principle.
    """
    feeds = [row["url"] for row in await store.list_personal_feeds()]
    if not feeds:
        return {"status": "personal digest not configured (no feeds)"}

    entries = await _collect_new(feeds, store)
    if not entries:
        return {"status": "no new items"}

    try:
        summary = await _summarize(entries, llm)
    except BudgetExceeded:
        log.warning("personal digest skipped: daily token budget hit")
        return {"status": "budget exceeded; skipped"}
    except LLMConfigError as exc:
        return {"status": f"digest brain not configured ({exc})"}

    channel_id = settings.personal_digest_channel_id
    if channel_id is not None:
        destination = client.get_channel(channel_id)
        if destination is None:
            return {"status": f"personal digest channel {channel_id} not found"}
    else:
        try:
            owner = await client.fetch_user(settings.owner_id)
            destination = await owner.create_dm()
        except discord.DiscordException:
            log.exception("failed to open a DM with the owner for the personal digest")
            return {"status": "DM failed; digest not delivered"}

    today = datetime.datetime.now(ZoneInfo(settings.tz)).strftime("%Y-%m-%d")
    embed = discord.Embed(title=f"Roger's personal digest — {today}", description=summary[:4096])
    try:
        await destination.send(embed=embed)
    except discord.DiscordException:
        log.exception("failed to deliver the personal digest")
        return {"status": "delivery failed; digest not sent"}

    # Mark seen only after a successful send, so a failed delivery retries the same items.
    await store.mark_seen([(entry["feed_url"], entry["id"]) for entry in entries])
    return {"status": "posted", "count": len(entries)}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_digest.py -v`
Expected: PASS (full file).

- [ ] **Step 5: Commit**

```bash
git add roger/brains/digest.py tests/test_digest.py
git commit -m "feat: run_personal_digest_job with DM-or-channel delivery"
```

---

### Task 4: Curation tools

**Files:**
- Modify: `roger/tools/schemas.py`
- Modify: `roger/tools/executors.py`
- Test: `tests/test_executors.py`

**Interfaces:**
- Consumes: `Store.list_personal_feeds`, `Store.add_personal_feed`, `Store.remove_personal_feed`
  (Task 1); the existing module-private `validate_feed(url)` and `_need_store(ctx)` in
  `executors.py` (unchanged, reused as-is).
- Produces: four new registry entries — `list_personal_feeds`, `suggest_personal_feeds`,
  `add_personal_feed`, `remove_personal_feed` — available to the admin brain automatically (it
  builds its tool list from `list(schemas.REGISTRY)`, so no `roger/brains/admin.py` change is
  needed).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_executors.py`, right after `test_feed_tool_without_store_raises_guard_error`
(end of the feed-tools section, before the `# ---... read-only server info` comment):

```python


async def test_add_personal_feed_validates_and_persists(feeds):
    feeds.responses["http://good"] = _good_feed(title="Good Blog", n=5)
    out = await executors.add_personal_feed(
        None, AddPersonalFeedArgs(url="http://good"), feeds.ctx
    )
    assert out["added"] is True
    assert out["title"] == "Good Blog"
    assert [f["url"] for f in await feeds.store.list_personal_feeds()] == ["http://good"]


async def test_add_personal_feed_rejects_non_feed(feeds):
    out = await executors.add_personal_feed(
        None, AddPersonalFeedArgs(url="http://nope"), feeds.ctx
    )
    assert out["added"] is False
    assert await feeds.store.count_personal_feeds() == 0


async def test_add_personal_feed_is_idempotent(feeds):
    feeds.responses["http://good"] = _good_feed()
    await executors.add_personal_feed(None, AddPersonalFeedArgs(url="http://good"), feeds.ctx)
    out = await executors.add_personal_feed(
        None, AddPersonalFeedArgs(url="http://good"), feeds.ctx
    )
    assert out["added"] is False
    assert out["note"] == "already in the personal feed list"


async def test_remove_personal_feed_hit_and_miss(feeds):
    feeds.responses["http://good"] = _good_feed()
    await executors.add_personal_feed(None, AddPersonalFeedArgs(url="http://good"), feeds.ctx)
    hit = await executors.remove_personal_feed(
        None, RemovePersonalFeedArgs(url="http://good"), feeds.ctx
    )
    assert hit["removed"] is True
    miss = await executors.remove_personal_feed(
        None, RemovePersonalFeedArgs(url="http://good"), feeds.ctx
    )
    assert miss["removed"] is False


async def test_list_personal_feeds_returns_current(feeds):
    feeds.responses["http://a"] = _good_feed(title="A")
    await executors.add_personal_feed(None, AddPersonalFeedArgs(url="http://a"), feeds.ctx)
    out = await executors.list_personal_feeds(None, ListPersonalFeedsArgs(), feeds.ctx)
    assert out["count"] == 1
    assert out["feeds"][0] == {"url": "http://a", "title": "A"}


async def test_suggest_personal_feeds_validates_without_persisting(feeds):
    feeds.responses["http://ok"] = _good_feed(title="OK", n=2)
    out = await executors.suggest_personal_feeds(
        None, SuggestPersonalFeedsArgs(urls=["http://ok"]), feeds.ctx
    )
    by_url = {c["url"]: c for c in out["candidates"]}
    assert by_url["http://ok"]["ok"] is True
    assert await feeds.store.count_personal_feeds() == 0  # suggest never writes


async def test_personal_feed_tool_without_store_raises_guard_error():
    with pytest.raises(GuardError):
        await executors.list_personal_feeds(None, ListPersonalFeedsArgs(), None)
```

Add the four new names to the existing `from roger.tools.schemas import (...)` block near the top of
`tests/test_executors.py` (wherever `AddFeedArgs`, `ListFeedsArgs`, `RemoveFeedArgs`,
`SuggestFeedsArgs` are currently imported) — `AddPersonalFeedArgs`, `ListPersonalFeedsArgs`,
`RemovePersonalFeedArgs`, `SuggestPersonalFeedsArgs`, kept alphabetical alongside the others.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_executors.py -k personal_feed -v`
Expected: FAIL — `ImportError` (the new arg classes don't exist yet), or once that's stubbed,
`AttributeError: module 'roger.tools.executors' has no attribute 'add_personal_feed'`.

- [ ] **Step 3: Add the four arg models and registry entries in `schemas.py`**

In `roger/tools/schemas.py`, add right after `RemoveFeedArgs` (after line 177):

```python


class ListPersonalFeedsArgs(ToolArgs):
    """No arguments — returns the owner's personal digest feed list."""


class SuggestPersonalFeedsArgs(ToolArgs):
    urls: list[str] = Field(min_length=1, max_length=8)  # candidate feed URLs to vet


class AddPersonalFeedArgs(ToolArgs):
    url: str  # RSS/Atom feed URL; validated live before it is stored


class RemovePersonalFeedArgs(ToolArgs):
    url: str  # exact stored URL (from list_personal_feeds)
```

Add to `REGISTRY` right after the `"remove_feed"` entry (after line 441, before `"set_presence"`):

```python
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
```

- [ ] **Step 4: Add the four executor functions in `executors.py`**

Add the four new names to the existing `from roger.tools.schemas import (...)` block (lines 28-58),
kept alphabetical: `AddPersonalFeedArgs` (between `AddMemberRoleArgs` and `AddReactionArgs`),
`ListPersonalFeedsArgs` (between `ListInvitesArgs` and `ListRoleMembersArgs`),
`RemovePersonalFeedArgs` (between `RemoveMemberRoleArgs` and `RemoveReactionArgs`),
`SuggestPersonalFeedsArgs` (after `SuggestFeedsArgs`).

Add the four functions right after `list_feeds` (after line 698, before the
`# ---... toys (self / read)` comment):

```python

# --------------------------------------------------------------------------- personal digest feeds


async def suggest_personal_feeds(
    guild: discord.Guild, args: SuggestPersonalFeedsArgs, ctx: ToolContext | None = None
) -> dict[str, Any]:
    candidates = await asyncio.gather(*(validate_feed(url) for url in args.urls))
    return {"candidates": list(candidates)}


async def add_personal_feed(
    guild: discord.Guild, args: AddPersonalFeedArgs, ctx: ToolContext | None = None
) -> dict[str, Any]:
    store = _need_store(ctx)
    checked = await validate_feed(args.url)
    if not checked["ok"]:
        return {"added": False, "url": args.url, "error": checked["error"]}
    added = await store.add_personal_feed(args.url, checked.get("title"))
    return {
        "added": added,
        "url": args.url,
        "title": checked.get("title"),
        "entries": checked.get("entries"),
        "note": None if added else "already in the personal feed list",
    }


async def remove_personal_feed(
    guild: discord.Guild, args: RemovePersonalFeedArgs, ctx: ToolContext | None = None
) -> dict[str, Any]:
    store = _need_store(ctx)
    removed = await store.remove_personal_feed(args.url)
    return {
        "removed": removed,
        "url": args.url,
        "note": None
        if removed
        else "no feed with that exact URL (call list_personal_feeds first)",
    }


async def list_personal_feeds(
    guild: discord.Guild, args: ListPersonalFeedsArgs, ctx: ToolContext | None = None
) -> dict[str, Any]:
    store = _need_store(ctx)
    rows = await store.list_personal_feeds()
    return {
        "feeds": [{"url": r["url"], "title": r["title"]} for r in rows],
        "count": len(rows),
    }
```

Add the four functions to the `EXECUTORS` dict right after `"list_feeds": list_feeds,`:

```python
    "suggest_personal_feeds": suggest_personal_feeds,
    "add_personal_feed": add_personal_feed,
    "remove_personal_feed": remove_personal_feed,
    "list_personal_feeds": list_personal_feeds,
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_executors.py -v`
Expected: PASS (full file).

Run: `pytest -q`
Expected: PASS (full suite — confirms the wider `REGISTRY`/`EXECUTORS` dicts didn't break an
existing test that enumerates them, e.g. an admin-tools-are-all-registered check).

- [ ] **Step 6: Commit**

```bash
git add roger/tools/schemas.py roger/tools/executors.py tests/test_executors.py
git commit -m "feat: add personal digest curation tools"
```

---

### Task 5: Scheduling and the ops alert

**Files:**
- Modify: `roger/bot.py`
- Test: `tests/test_ops.py`

**Interfaces:**
- Consumes: `run_personal_digest_job`, `seed_personal_feeds_if_empty` (Task 3);
  `settings.personal_feeds`, `settings.personal_digest_hour` (Task 2); the existing `_DAY_S`
  constant and `self._ops.alert(key, message, cooldown_s=...)` method (unchanged).
- Produces: `_personal_digest_problem(status: str) -> str | None` (module-level, pure — mirrors
  `_digest_problem`). No later task depends on anything new here.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_ops.py`, right after `test_digest_problem_flags_failures` (after line 71, before
the gigabrain-problem tests):

```python


def test_personal_digest_problem_none_for_success_statuses():
    assert _personal_digest_problem("posted") is None
    assert _personal_digest_problem("no new items") is None


def test_personal_digest_problem_flags_failures():
    assert _personal_digest_problem("personal digest not configured (no feeds)") is not None
    assert _personal_digest_problem("DM failed; digest not delivered") is not None
    assert _personal_digest_problem("budget exceeded; skipped") is not None
```

Update the import at the top of `tests/test_ops.py` (line 3) to add `_personal_digest_problem`:

```python
from roger.bot import (
    OpsNotifier,
    _budget_alert,
    _digest_problem,
    _gigabrain_problem,
    _personal_digest_problem,
)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ops.py -k personal_digest_problem -v`
Expected: FAIL — `ImportError: cannot import name '_personal_digest_problem'`.

- [ ] **Step 3: Add the problem helper**

In `roger/bot.py`, add right after `_digest_problem` (after line 418, before the
`# Gigabrain statuses...` comment):

```python

# Personal digest statuses that mean "ran fine, nothing to flag"; anything else is worth an ops
# ping — same OK-prefix shape as the public digest.
_PERSONAL_DIGEST_OK_PREFIXES = ("posted", "no new items")


def _personal_digest_problem(status: str) -> str | None:
    """The personal digest status if it signals a problem worth alerting on, else None (pure)."""
    if any(status.startswith(prefix) for prefix in _PERSONAL_DIGEST_OK_PREFIXES):
        return None
    return status
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_ops.py -v`
Expected: PASS.

- [ ] **Step 5: Wire the scheduled loop**

Update the import at the top of `roger/bot.py` (line 32):

```python
from roger.brains.digest import (
    run_digest_job,
    run_personal_digest_job,
    seed_feeds_if_empty,
    seed_personal_feeds_if_empty,
)
```

In `setup_hook` (around line 466-468), add right after the existing feed-seeding block:

```python
        seeded = await seed_feeds_if_empty(self.store, self.settings)
        if seeded:
            log.info("seeded %d feed(s) from DIGEST_FEEDS into the store", seeded)
        personal_seeded = await seed_personal_feeds_if_empty(self.store, self.settings)
        if personal_seeded:
            log.info(
                "seeded %d feed(s) from PERSONAL_DIGEST_FEEDS into the store", personal_seeded
            )
```

In `setup_hook`, add right after the existing digest-loop start block (after line 485, before the
gigabrain block):

```python
        # Turned on by configuring at least one seed feed — DM delivery needs no channel to be set,
        # unlike the public digest's channel-required gate.
        if self.settings.personal_digest_feeds:
            self._personal_digest_loop.change_interval(
                time=datetime.time(
                    hour=self.settings.personal_digest_hour, tzinfo=ZoneInfo(self.settings.tz)
                )
            )
            self._personal_digest_loop.start()
            log.info(
                "personal digest scheduled daily at %02d:00 %s",
                self.settings.personal_digest_hour,
                self.settings.tz,
            )
```

Add the loop method right after `_before_digest` (after line 664, before the `_gigabrain_loop`
definition):

```python
    @tasks.loop(time=datetime.time(hour=7))
    async def _personal_digest_loop(self) -> None:
        result = await run_personal_digest_job(
            client=self, settings=self.settings, llm=self.llm, store=self.store
        )
        status = str(result.get("status", ""))
        log.info("scheduled personal digest: %s", status)
        problem = _personal_digest_problem(status)
        if problem:
            await self._ops.alert(
                f"personal_digest:{time.strftime('%Y-%m-%d')}",
                f"⚠️ **personal digest problem** — {problem}",
                cooldown_s=_DAY_S,
            )

    @_personal_digest_loop.before_loop
    async def _before_personal_digest(self) -> None:
        await self.wait_until_ready()
```

- [ ] **Step 6: Run the full suite**

Run: `pytest -q`
Expected: PASS (full suite).

Run: `ruff check .`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add roger/bot.py tests/test_ops.py
git commit -m "feat: schedule the personal digest and alert on failure"
```

---

### Task 6: Docs

**Files:**
- Modify: `ARCHITECTURE.md`
- Modify: `ROADMAP.md`
- Modify: `README.md`

No test — this is a prose-only task. Verification is a full-suite run plus a read-through.

- [ ] **Step 1: Update `ARCHITECTURE.md` §9**

Add a new bullet to the `## §9 Digest brain` section, right after the "Exactly-once posting" bullet
(after line 259):

```markdown
- **A personal, DM'd sibling.** `run_personal_digest_job` is the same mechanism — fetch, dedupe,
  summarize — pointed at a second, separately-curated `personal_feeds` list, and delivered to the
  owner only (`PERSONAL_DIGEST_CHANNEL_ID` if set, else a DM — same fallback shape Giga Brain's
  periodic check-in uses, §12). It shares the `digest` brain's model and daily budget; it's a
  second job, not a second brain. Curated the same way — `suggest_personal_feeds` / `add_personal_feed`
  / `remove_personal_feed` / `list_personal_feeds` mirror the public digest's four curation tools
  exactly.
```

- [ ] **Step 2: Update `ARCHITECTURE.md` §10**

Add a row to the persistence table (after the `feeds` row, around line 274):

```markdown
| `personal_feeds` | The owner's personal digest feed list, curated separately from `feeds` (§9) |
```

- [ ] **Step 3: Flip `ROADMAP.md` item 1 to shipped**

In `ROADMAP.md`, change the item 1 heading from:

```markdown
## 1. Personal digest — **S/M** — *spec written*
```

to:

```markdown
## 1. Personal digest — **S/M** — *shipped*
```

- [ ] **Step 4: Update `README.md`'s Status section**

In the `## Status` section's Digest bullet, add one sentence noting the personal digest. Find the
bullet starting `- **Digest** — a scheduled daily RSS/Atom summary...` and append after its existing
sentence:

```markdown
  A second, privately-curated feed list can also be DM'd to the owner only
  (`PERSONAL_DIGEST_FEEDS`), on its own schedule.
```

- [ ] **Step 5: Run the full suite one more time**

Run: `pytest -q`
Expected: PASS.

Run: `ruff check .`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add ARCHITECTURE.md ROADMAP.md README.md
git commit -m "docs: personal digest in ARCHITECTURE, ROADMAP, README"
```
