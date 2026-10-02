"""Developing-story memory, historical previews, and evidence gates."""

import datetime
import json
import time
from types import SimpleNamespace

import pytest

from roger.brains import stories
from roger.brains.curated import DraftError, preview_curated_job, run_curated_job
from roger.scout_source import ScoutBatch
from roger.store import CURATED_STORY_DECISION_LIMIT, CURATED_STORY_LIMIT, Store

NOW = datetime.datetime(2026, 10, 1, 16, tzinfo=datetime.UTC)

# Captured primary-source excerpts. Each string is used once as fixture source text; generated
# decisions refer to the same object so the fixture does not duplicate source quotations.
JEV = "Our first public model is Jev, available today in early access."
KEV = (
    "Jev-inspired decision model. Typed questions in, calibrated probabilities out, one forward "
    "pass."
)
KEV_BENCH = "so this is a shared-item comparison, not a controlled ablation."
KEV_LICENSE = "Apache License\n                           Version 2.0, January 2004"
GROK = (
    "Grok Bot gives you Bots you can keep around: AI teammates with names, jobs, and context "
    "that compounds over time."
)
DOTS_LAUNCH = "Dots are always-on agents that take on ongoing responsibility."
DOTS_ROLLOUT = "Dots are rolling out gradually to eligible accounts."
DOTS_REPEAT = (
    "Dots are rolling out gradually. You may not see dots immediately, even if your plan is "
    "eligible."
)
SYNTH_FIRST = "The synthetic release is available to every test account in this fixture."
SYNTH_SECOND = "The synthetic release now limits access to a staged test cohort."
SYNTH_FILLER = " Synthetic fixture context is intentionally long enough for production admission."
JEV_URL = "https://typesafe.ai/blog/introducing-system-one-models-and-jev"
KEV_URL = (
    "https://github.com/jaredpalmer/kev/blob/"
    "61457121805df6a9e089ff1b1d15548be9d0e810/README.md"
)
KEV_LICENSE_URL = (
    "https://github.com/jaredpalmer/kev/blob/"
    "61457121805df6a9e089ff1b1d15548be9d0e810/LICENSE"
)
GROK_URL = "https://docs.x.ai/grok-bot/overview"
DOTS_LAUNCH_URL = "https://learn.chatgpt.com/docs/whats-new/devday-2026"
DOTS_URL = "https://learn.chatgpt.com/docs/dots"


def _sid(name, url=None):
    return stories.story_id(_entry(name, "fixture source", url=url))


def _entry(
    name, quote, *, observed_at="2026-10-01T12:00:00+00:00", url=None, published=None,
    production=False,
):
    text = quote + (SYNTH_FILLER * 6 if production else "")
    return {
        "feed_url": "scout:fixtures",
        "id": name,
        "title": name,
        "summary": "",
        "published": time.strptime(published, "%Y-%m-%d") if published else None,
        "relevance": 1,
        "matched": [],
        "observed_at": observed_at,
        "article": {
            "status": "ok", "url": url or f"https://example.test/{name}", "text": text,
        },
    }


def _citation(source, quote):
    return {"source": source, "quote": quote}


def _grounded(text, source=None, quote=None):
    citations = [] if source is None else [_citation(source, quote)]
    return {"text": text, "citations": citations}


def _decision(
    action, ids, source_quotes, *, correction_of=None, reason="Editorial decision.",
    headline="A source-backed development", fact="The cited source states the development.",
    why="The source gives a concrete change to evaluate.", change=None, stage=None,
):
    if action in {"hold", "skip"}:
        return {
            "action": action, "reason": _grounded(reason), "change": _grounded("", None),
            "story_ids": ids, "claims": [], "unresolved_questions": [reason],
            "correction_of": correction_of,
        }
    citations = [_citation(index, quote) for index, quote in source_quotes]
    stage = stage or (
        "rollout" if any("rolling out" in quote for _, quote in source_quotes) else "demonstrated"
    )
    return {
        "action": action,
        "reason": {"text": "The evidence supports a material report.", "citations": citations},
        "change": {"text": change or "The cited evidence changes the prior picture.",
                   "citations": citations},
        "story_ids": ids,
        "claims": [
            {"field": "headline", "text": headline, "citations": citations,
             "mutable": False},
            {"field": "stage", "text": stage, "citations": citations, "mutable": False},
            {"field": "fact", "text": fact,
             "citations": citations, "mutable": False},
            {"field": "why", "text": why,
             "citations": citations, "mutable": False},
        ],
        "unresolved_questions": [],
        "correction_of": correction_of,
    }


def _review(decision):
    publishable = decision["action"] in {"publish", "update", "combine"}
    return {
        "supported": [True] * (2 + len(decision["claims"])),
        "material_change": publishable,
        "action_supported": True,
        "chronology_supported": True,
        "relationship_supported": True,
        "freshness_supported": True,
        "correction_supported": True,
    }


class FakeLLM:
    def __init__(self, decision):
        self.responses = [decision, _review(decision)]
        self.calls = []

    async def complete(self, brain, messages, *, curated_review=False):
        self.calls.append((brain, curated_review, messages))
        value = self.responses.pop(0)
        content = json.dumps(value)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class QueueLLM:
    def __init__(self, *values):
        self.values = list(values)

    async def complete(self, brain, messages, *, curated_review=False):
        value = self.values.pop(0)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(value)))]
        )


class RecordingQueueLLM(QueueLLM):
    def __init__(self, *values):
        super().__init__(*values)
        self.calls = []

    async def complete(self, brain, messages, *, curated_review=False):
        self.calls.append((brain, curated_review, messages))
        return await super().complete(brain, messages, curated_review=curated_review)


@pytest.mark.parametrize(
    ("name", "entries", "decision", "expected", "phrase", "urls"),
    [
        (
            "launch to open alternative with qualified benchmark",
            [
                _entry("jev", JEV, published="2026-09-15", url=JEV_URL),
                # The pinned commit is a 2026-09-20 snapshot, not a publication date.
                _entry("kev", f"{KEV} {KEV_BENCH}", url=KEV_URL),
                _entry("kev-license", KEV_LICENSE, url=KEV_LICENSE_URL),
            ],
            _decision(
                "combine", [_sid("jev", JEV_URL), _sid("kev", KEV_URL)],
                [(1, JEV), (2, KEV), (2, KEV_BENCH), (3, KEV_LICENSE)],
                headline="Jev is in early access while Kev documents a Jev-inspired model",
                fact="Kev repository code carries the Apache 2.0 license.",
                why=("Kev is a concrete alternative to compare, while its shared-item result is "
                     "not a controlled ablation."),
                change=("Kev adds a documented Jev-inspired alternative and qualified comparison "
                        "evidence."),
            ),
            "draft",
            "not a controlled ablation",
            {"typesafe.ai", "github.com"},
        ),
        (
            "agent announcement and limited rollout stay distinct",
            [
                # The 2026-09-21 document update is not an established launch date.
                _entry("grok-bot", GROK, url=GROK_URL),
                _entry("dots", f"{DOTS_LAUNCH} {DOTS_ROLLOUT}", published="2026-09-29",
                       url=DOTS_LAUNCH_URL),
            ],
            _decision(
                "combine", [_sid("grok-bot", GROK_URL), _sid("dots", DOTS_LAUNCH_URL)],
                [(1, GROK), (2, DOTS_LAUNCH), (2, DOTS_ROLLOUT)],
                headline="Grok Bot and ChatGPT Dots are distinct persistent-agent products",
                fact="The ChatGPT source says Dots are rolling out gradually.",
                why="The shared beat does not make the two launches one event.", stage="rollout",
            ),
            "draft",
            "distinct persistent-agent products",
            {"docs.x.ai", "learn.chatgpt.com"},
        ),
        (
            "repeated rollout evidence",
            [_entry("dots", DOTS_REPEAT, url=DOTS_URL)],
            _decision("hold", [_sid("dots", DOTS_URL)], [],
                      reason="The later capture repeats gradual rollout without a new stage."),
            "no-post decision",
            "repeats gradual rollout",
            {"learn.chatgpt.com"},
        ),
        (
            "related but distinct events",
            [_entry("grok-bot", GROK, url=GROK_URL),
             _entry("dots", DOTS_LAUNCH, url=DOTS_LAUNCH_URL)],
            _decision(
                "combine", [_sid("grok-bot", GROK_URL), _sid("dots", DOTS_LAUNCH_URL)],
                [(1, GROK), (2, DOTS_LAUNCH)],
                headline="Two persistent-agent announcements remain separate stories",
                fact="Grok Bot names AI teammates; Dots take ongoing responsibility.",
                why="They can share one report without collapsing their event identities.",
            ),
            "draft",
            "separate stories",
            {"docs.x.ai", "learn.chatgpt.com"},
        ),
        (
            "insufficient comparison",
            [_entry("jev", JEV, url=JEV_URL)],
            _decision("hold", [_sid("jev", JEV_URL)], [],
                      reason="A second source for the requested comparison is missing."),
            "no-post decision",
            "comparison is missing",
            {"typesafe.ai"},
        ),
    ],
)
async def test_historical_captured_source_previews(
    name, entries, decision, expected, phrase, urls,
):
    history = {"stories": [], "delivered": []}
    llm = FakeLLM(decision)
    actual, sources = await stories.decide(
        entries, history, llm, now=NOW, max_age_hours=36, captured_source_preview=True,
    )
    result = stories.preview(actual, sources, history)
    assert result["status"] == expected, name
    assert result["story_ids"] == decision["story_ids"]
    rendered = json.dumps(result)
    assert phrase in rendered
    assert urls == {source["url"].split("/")[2] for source in result["sources"]}
    assert all(source["observed_at"] == "2026-10-01T12:00:00+00:00"
               for source in result["sources"])
    assert len(llm.calls) == 2
    for claim in result["claims"]:
        for citation in claim["citations"]:
            source = result["sources"][citation["source"] - 1]
            assert citation["quote"] in source["excerpt"]


async def test_correction_names_confirmed_error_keeps_history_and_links_prior_message(tmp_path):
    store = await Store(str(tmp_path / "roger.db")).open()
    try:
        earlier = _decision(
            "publish", [_sid("dots")], [(1, DOTS_LAUNCH)],
            headline="Synthetic prior error: all eligible accounts have immediate Dots access",
            fact="Synthetic seeded history claimed immediate access for every eligible account.",
            change="Synthetic seeded first report.", stage="announcement",
        )
        earlier_sources = stories.prepare_captured_sources(
            [_entry("dots", DOTS_LAUNCH)], now=NOW, max_age_hours=36,
        )
        earlier_parsed = stories.parse_decision(
            json.dumps(earlier), earlier_sources, {"stories": [], "delivered": []},
        )
        earlier_record = earlier_parsed.record() | {
            "event_key": stories.event_key(earlier_parsed, earlier_sources)
        }
        first_id = await store.record_curated_story_decision(
            earlier_record, earlier_sources, now=NOW.timestamp(),
        )
        generation = await store.claim_curated_check(NOW.timestamp())
        delivery = await store.claim_curated(
            "2026-10-01", "scout:fixtures", "dots",
            event_key=earlier_record["event_key"],
            max_posts=4, spacing_seconds=0, generation=generation, now=NOW.timestamp(),
            story_decision_id=first_id,
        )
        await store.mark_curated_sent(
            delivery, 111, message_url="https://discord.com/channels/1/2/111",
        )
        await store.release_curated_check(generation)

        history = await store.curated_story_context()
        correction = _decision(
            "update", [_sid("dots")], [(1, DOTS_REPEAT)], correction_of=first_id,
            headline="Correction: Dots access is gradual",
            fact="The current Dots page says eligible users may not see Dots immediately.",
            stage="rollout",
        )
        correction["change"] = _grounded(
            "Correction: the prior report wrongly said all eligible accounts had immediate access; "
            "the source says rollout is gradual.", 1, DOTS_REPEAT,
        )
        llm = FakeLLM(correction)
        actual, sources = await stories.decide(
            [_entry("dots", DOTS_REPEAT)], history, llm, now=NOW, max_age_hours=36,
            captured_source_preview=True,
        )
        preview = stories.preview(actual, sources, history)
        assert preview["correction_of"] == first_id
        assert preview["earlier_confirmed_report"].endswith("/111")
        record = actual.record() | {"event_key": stories.event_key(actual, sources)}
        second_id = await store.record_curated_story_decision(record, sources,
                                                               now=NOW.timestamp() + 1)
        assert second_id > first_id
        assert len(await store.curated_story_decisions()) == 2
    finally:
        await store.close()


async def test_pending_and_unlinked_decisions_are_not_delivered_history(tmp_path):
    store = await Store(str(tmp_path / "roger.db")).open()
    try:
        source = stories.prepare_captured_sources(
            [_entry("dots", DOTS_LAUNCH)], now=NOW, max_age_hours=36,
        )
        decision = stories.parse_decision(
            json.dumps(_decision("publish", [_sid("dots")], [(1, DOTS_LAUNCH)])), source,
            {"stories": [], "delivered": []},
        )
        record = decision.record() | {"event_key": stories.event_key(decision, source)}
        unlinked = await store.record_curated_story_decision(
            record, source, now=NOW.timestamp(),
        )
        pending = await store.record_curated_story_decision(
            record, source, now=NOW.timestamp() + 1,
        )
        generation = await store.claim_curated_check(NOW.timestamp())
        await store.claim_curated(
            "2026-10-01", "scout:fixtures", "dots", event_key=record["event_key"],
            max_posts=4, spacing_seconds=0, generation=generation, now=NOW.timestamp(),
            story_decision_id=pending,
        )
        history = await store.curated_story_context()
        assert history["delivered"] == []
        assert {row["id"] for row in await store.curated_story_decisions()} == {unlinked, pending}
    finally:
        await store.close()


async def test_developing_preview_spends_calls_without_state_mutation(tmp_path, monkeypatch):
    entry = _entry("dots", DOTS_ROLLOUT, production=True)

    async def collect(*args, **kwargs):
        assert kwargs["include_seen"] is True
        assert kwargs["prefer_latest_source"] is True
        return ScoutBatch([entry], "fixture-run", 1.0, "")

    monkeypatch.setattr("roger.brains.curated.collect_from_scout", collect)
    decision = _decision("publish", [_sid("dots")], [(1, DOTS_ROLLOUT)])
    llm = FakeLLM(decision)
    store = await Store(str(tmp_path / "roger.db")).open()
    settings = SimpleNamespace(
        scout_digest_path=tmp_path, scout_max_age_hours=36,
        curated_models=["draft"], curated_review_models=["review"],
    )
    try:
        result = await preview_curated_job(
            settings=settings, llm=llm, store=store, developing_stories=True,
        )
        assert result["status"] == "draft" and len(llm.calls) == 2
        for table in (
            "curated_story", "curated_story_decision", "curated_delivery", "seen",
            "curated_observation",
        ):
            cursor = await store._conn.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608
            assert (await cursor.fetchone())[0] == 0
    finally:
        await store.close()


async def test_long_source_url_is_rejected_before_model_or_delivery(tmp_path, monkeypatch):
    entry = _entry(
        "long-url", SYNTH_FIRST, production=True,
        url="https://example.test/" + "segment" * 30,
    )

    async def collect(*args, **kwargs):
        return ScoutBatch([entry], "fixture-run", 1.0, "")

    class NoLLM:
        async def complete(self, *args, **kwargs):
            raise AssertionError("an unrenderable source must not reach the model")

    monkeypatch.setattr("roger.brains.curated.collect_from_scout", collect)
    settings = SimpleNamespace(
        curated_channel_id=42, curated_developing_stories=True, guild_id=7, tz="UTC",
        curated_max_posts_per_day=2, curated_min_spacing_minutes=0,
        curated_max_observations_per_day=8, curated_models=["draft"],
        curated_review_models=["review"], scout_digest_path=tmp_path,
        scout_max_age_hours=36,
    )
    store = await Store(str(tmp_path / "roger.db")).open()
    try:
        result = await run_curated_job(
            client=SimpleNamespace(get_channel=lambda _: SimpleNamespace(send=lambda **_: None)),
            settings=settings, llm=NoLLM(), store=store, now=NOW,
        )
        cursor = await store._conn.execute("SELECT COUNT(*) FROM curated_delivery")
        assert result["status"] == "no post-worthy items"
        assert (await cursor.fetchone())[0] == 0
    finally:
        await store.close()


async def test_opt_in_job_posts_changed_same_source_through_existing_delivery_ledger(
    tmp_path, monkeypatch,
):
    versions = [
        _entry("same", SYNTH_FIRST, production=True),
        _entry("same", SYNTH_SECOND, production=True),
    ]

    async def collect(*args, **kwargs):
        return ScoutBatch([versions.pop(0)], "fixture-run", 1.0, "")

    monkeypatch.setattr("roger.brains.curated.collect_from_scout", collect)
    first = _decision("publish", [_sid("same")], [(1, SYNTH_FIRST)])
    second = _decision("update", [_sid("same")], [(1, SYNTH_SECOND)])
    llm = QueueLLM(first, _review(first), second, _review(second))

    class Channel:
        def __init__(self):
            self.sent = []

        async def send(self, *, embed, allowed_mentions):
            self.sent.append(embed)
            return SimpleNamespace(id=100 + len(self.sent))

    channel = Channel()
    settings = SimpleNamespace(
        curated_channel_id=42, curated_developing_stories=True, guild_id=7, tz="UTC",
        curated_max_posts_per_day=2, curated_min_spacing_minutes=0,
        curated_max_observations_per_day=8, curated_models=["draft"],
        curated_review_models=["review"], scout_digest_path=tmp_path,
        scout_max_age_hours=36,
    )
    store = await Store(str(tmp_path / "roger.db")).open()
    client = SimpleNamespace(get_channel=lambda _: channel)
    try:
        posted = await run_curated_job(
            client=client, settings=settings, llm=llm, store=store, now=NOW,
        )
        updated = await run_curated_job(
            client=client, settings=settings, llm=llm, store=store,
            now=NOW + datetime.timedelta(minutes=5),
        )
        assert posted["status"] == updated["status"] == "posted"
        assert len(channel.sent) == 2
        assert "What changed:" in channel.sent[1].description
        assert channel.sent[1].fields[0].value.endswith("/101")
        history = await store.curated_story_context()
        assert len(history["delivered"]) == 2
        assert all(item["message_url"] for item in history["delivered"])
    finally:
        await store.close()


def test_outdated_mutable_claim_is_rejected_even_with_fresh_unrelated_digest():
    old = _entry("dots", DOTS_REPEAT, observed_at="2026-09-20T12:00:00+00:00")
    fresh = _entry("grok-bot", GROK)
    sources = stories.prepare_captured_sources([old, fresh], now=NOW, max_age_hours=36)
    decision = _decision("publish", [_sid("dots")], [(1, DOTS_REPEAT)])
    decision["claims"][2]["mutable"] = True
    with pytest.raises(DraftError, match="fresh source"):
        stories.parse_decision(json.dumps(decision), sources, {"stories": [], "delivered": []})


def test_story_fingerprint_ignores_timestamp_only_refresh_but_detects_changed_excerpt():
    settings = SimpleNamespace(curated_models=["draft"], curated_review_models=["review"])
    history = {"stories": [], "delivered": []}
    first = stories.prepare_captured_sources(
        [_entry("dots", DOTS_LAUNCH, observed_at="2026-10-01T10:00:00+00:00")],
        now=NOW, max_age_hours=36,
    )
    refreshed = stories.prepare_captured_sources(
        [_entry("dots", DOTS_LAUNCH, observed_at="2026-10-01T15:00:00+00:00")],
        now=NOW, max_age_hours=36,
    )
    changed = stories.prepare_captured_sources(
        [_entry("dots", DOTS_REPEAT, observed_at="2026-10-01T15:00:00+00:00")],
        now=NOW, max_age_hours=36,
    )
    assert stories.input_fingerprint(first, history, settings) == stories.input_fingerprint(
        refreshed, history, settings,
    )
    assert stories.input_fingerprint(first, history, settings) != stories.input_fingerprint(
        changed, history, settings,
    )


async def test_story_records_restart_and_enforce_count_caps(tmp_path):
    path = str(tmp_path / "roger.db")
    store = await Store(path).open()
    source = [{"source_id": "s", "story_id": "story", "excerpt": JEV}]
    try:
        rows = [
            ("hold", "{}", "{}", "[]", "[]", "[]", None, None, float(index))
            for index in range(CURATED_STORY_DECISION_LIMIT + 2)
        ]
        await store._conn.executemany(
            "INSERT INTO curated_story_decision (action, reason_json, change_json, story_ids_json, "
            "claims_json, sources_json, event_key, correction_of, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows,
        )
        await store._conn.executemany(
            "INSERT INTO curated_story (story_id, data_json, ts) VALUES (?, '{}', ?)",
            [(f"old-{index}", float(index)) for index in range(CURATED_STORY_LIMIT + 2)],
        )
        await store._conn.commit()
        await store.record_curated_story_decision(
            {"action": "hold", "reason": _grounded("r"), "change": _grounded("", None),
             "story_ids": ["story"], "event_key": None,
             "claims": [], "unresolved_questions": [], "correction_of": None},
            source, now=1_000,
        )
        decision_count = await store._conn.execute(
            "SELECT COUNT(*) FROM curated_story_decision"
        )
        story_count = await store._conn.execute("SELECT COUNT(*) FROM curated_story")
        assert (await decision_count.fetchone())[0] == CURATED_STORY_DECISION_LIMIT
        assert (await story_count.fetchone())[0] == CURATED_STORY_LIMIT
    finally:
        await store.close()
    reopened = await Store(path).open()
    try:
        context = await reopened.curated_story_context()
        assert any(row["story_id"] == "story" for row in context["stories"])
    finally:
        await reopened.close()


async def test_story_decision_ids_are_not_reused_after_delete_and_reopen(tmp_path):
    path = str(tmp_path / "roger.db")
    store = await Store(path).open()
    first_source = stories.prepare_captured_sources(
        [_entry("first", SYNTH_FIRST)], now=NOW, max_age_hours=36,
    )
    first = stories.parse_decision(
        json.dumps(_decision(
            "publish", [first_source[0]["story_id"]], [(1, SYNTH_FIRST)],
        )), first_source, {"stories": [], "delivered": []},
    )
    first_record = first.record() | {"event_key": stories.event_key(first, first_source)}
    first_id = await store.record_curated_story_decision(
        first_record, first_source, now=NOW.timestamp(),
    )
    generation = await store.claim_curated_check(NOW.timestamp())
    delivery = await store.claim_curated(
        "2026-10-01", "scout:fixtures", "first", event_key=first_record["event_key"],
        max_posts=5, spacing_seconds=0, generation=generation, now=NOW.timestamp(),
        story_decision_id=first_id,
    )
    await store.mark_curated_sent(delivery, 1)
    await store._conn.execute("DELETE FROM curated_story_decision")
    await store._conn.commit()
    await store.close()

    reopened = await Store(path).open()
    try:
        second_source = stories.prepare_captured_sources(
            [_entry("second", SYNTH_SECOND)], now=NOW, max_age_hours=36,
        )
        second = stories.parse_decision(
            json.dumps(_decision(
                "hold", [second_source[0]["story_id"]], [], reason="No material change.",
            )), second_source, {"stories": [], "delivered": []},
        )
        second_id = await reopened.record_curated_story_decision(
            second.record() | {"event_key": None}, second_source, now=NOW.timestamp() + 1,
        )
        assert second_id > first_id
        assert (await reopened.curated_story_context(
            [second_source[0]["story_id"]]
        ))["delivered"] == []
    finally:
        await reopened.close()


@pytest.mark.parametrize("mutation", ["action", "story_id", "field"])
def test_untrusted_unhashable_model_shapes_raise_draft_error(mutation):
    sources = stories.prepare_captured_sources(
        [_entry("dots", DOTS_LAUNCH)], now=NOW, max_age_hours=36,
    )
    decision = _decision("publish", [_sid("dots")], [(1, DOTS_LAUNCH)])
    if mutation == "action":
        decision["action"] = []
    elif mutation == "story_id":
        decision["story_ids"] = [{}]
    else:
        decision["claims"][0]["field"] = []
    with pytest.raises(DraftError):
        stories.parse_decision(json.dumps(decision), sources, {"stories": [], "delivered": []})


def test_story_identity_dedupes_url_aliases_and_hashes_long_fallbacks():
    first = _entry("a", JEV, url="https://EXAMPLE.test/story#one")
    mirror = {**_entry("different-native-id", JEV, url="https://example.test/story#two"),
              "feed_url": "scout:mirror"}
    assert stories.story_id(first) == stories.story_id(mirror)
    left = {"feed_url": "f", "id": "a" * 500, "article": {}}
    right = {"feed_url": "f", "id": "a" * 499 + "b", "article": {}}
    assert stories.story_id(left) != stories.story_id(right)
    assert len(stories.story_id(left)) < 80


async def test_new_url_source_can_update_a_reviewed_prior_story_without_event_hint():
    prior_entry = _entry("dots-launch", DOTS_LAUNCH, url=DOTS_LAUNCH_URL, production=True)
    prior_source = stories.prepare_sources([prior_entry], now=NOW, max_age_hours=36)[0]
    history = {
        "stories": [{"story_id": prior_source["story_id"], "ts": NOW.timestamp(), "data": {
            "sources": [prior_source], "unresolved_questions": [], "last_decision_id": 1,
        }}],
        "delivered": [{
            "id": 1, "action": "publish", "reason": _grounded("seed"),
            "change": _grounded("seed"), "story_ids": [prior_source["story_id"]],
            "claims": [], "sources": [prior_source], "correction_of": None,
            "message_id": "10", "message_url": "https://discord.com/channels/1/2/10",
            "ts": NOW.timestamp(),
        }],
    }
    current = _entry("dots-followup", DOTS_REPEAT, url=DOTS_URL, production=True)
    decision = _decision(
        "update", [prior_source["story_id"]], [(1, DOTS_REPEAT)],
        headline="The Dots page confirms access remains gradual",
        fact="Eligible accounts may still not see Dots immediately.", stage="rollout",
    )
    actual, bundle = await stories.decide(
        [current], history, FakeLLM(decision), now=NOW, max_age_hours=36,
    )
    assert actual.story_ids == (prior_source["story_id"],)
    assert bundle[0]["story_id"] != actual.story_ids[0]
    assert {source["url"] for source in bundle} == {DOTS_URL, DOTS_LAUNCH_URL}


async def test_independent_review_catches_stale_current_claim_marked_nonmutable():
    old = _entry("dots", DOTS_REPEAT, observed_at="2026-09-20T12:00:00+00:00")
    fresh = _entry("grok", GROK)
    publish = _decision("publish", [_sid("dots")], [(1, DOTS_REPEAT), (2, GROK)])
    publish["claims"][2]["text"] = "Dots access is currently gradual."
    rejected = _review(publish) | {"freshness_supported": False}
    hold = _decision(
        "hold", [_sid("dots")], [], reason="Current Dots availability lacks fresh evidence.",
    )
    llm = QueueLLM(publish, rejected, hold, _review(hold))
    actual, _ = await stories.decide(
        [old, fresh], {"stories": [], "delivered": []}, llm, now=NOW, max_age_hours=36,
        captured_source_preview=True,
    )
    assert actual.action == "hold" and not llm.values


async def test_review_receives_the_exact_cited_span_and_blocks_irrelevant_support():
    irrelevant = "This source also describes an unrelated archival maintenance detail."
    actual_support = "The synthetic release is now available to the staged test cohort."
    entry = _entry("release", f"{irrelevant} {actual_support}")
    publish = _decision(
        "publish", [_sid("release")], [(1, irrelevant)],
        headline="The synthetic release is available to the staged cohort",
        fact="The release is now available to the staged test cohort.",
    )
    rejected = _review(publish)
    rejected["supported"][4] = False
    hold = _decision(
        "hold", [_sid("release")], [], reason="The chosen quote does not support availability.",
    )
    llm = RecordingQueueLLM(publish, rejected, hold, _review(hold))
    decision, _ = await stories.decide(
        [entry], {"stories": [], "delivered": []}, llm, now=NOW, max_age_hours=36,
        captured_source_preview=True,
    )
    review_payload = json.loads(llm.calls[1][2][-1]["content"])
    assert review_payload["decision"]["citation_pool"] == [
        {"source": 1, "quote": irrelevant}
    ]
    assert review_payload["decision"]["items"][0]["citations"] == [1]
    assert decision.action == "hold"


def test_correction_target_must_match_story_and_use_fresh_change_evidence():
    source = stories.prepare_captured_sources(
        [_entry("a", DOTS_REPEAT)], now=NOW, max_age_hours=36,
    )
    sid = source[0]["story_id"]
    other = _sid("b")
    history = {"stories": [], "delivered": [
        {"id": 1, "story_ids": [sid]}, {"id": 2, "story_ids": [other]},
    ]}
    wrong = _decision("update", [sid], [(1, DOTS_REPEAT)], correction_of=2)
    with pytest.raises(DraftError, match="different story"):
        stories.parse_decision(json.dumps(wrong), source, history)
    stale_source = stories.prepare_captured_sources(
        [_entry("a", DOTS_REPEAT, observed_at="2026-09-20T12:00:00+00:00")],
        now=NOW, max_age_hours=36,
    )
    stale = _decision("update", [sid], [(1, DOTS_REPEAT)], correction_of=1)
    with pytest.raises(DraftError, match="fresh change"):
        stories.parse_decision(json.dumps(stale), stale_source, history)


def test_public_reason_requires_exact_source_citation():
    source = stories.prepare_captured_sources(
        [_entry("dots", DOTS_REPEAT)], now=NOW, max_age_hours=36,
    )
    decision = _decision("publish", [source[0]["story_id"]], [(1, DOTS_REPEAT)])
    decision["reason"]["citations"] = []
    with pytest.raises(DraftError, match="invalid citations"):
        stories.parse_decision(
            json.dumps(decision), source, {"stories": [], "delivered": []},
        )


def test_max_length_retained_sources_fit_projected_model_history():
    entries = [
        _entry(str(index), f"Evidence {index} " + "x" * 1_700,
               url=f"https://example.test/{index}", production=False)
        for index in range(3)
    ]
    sources = stories.prepare_sources(entries, now=NOW, max_age_hours=36)
    delivered = {
        "id": 1, "action": "combine", "reason": _grounded("reason"),
        "change": _grounded("change"), "story_ids": [s["story_id"] for s in sources],
        "claims": [], "sources": sources, "correction_of": None, "message_id": "1",
        "message_url": "https://discord.com/channels/1/2/1", "ts": NOW.timestamp(),
    }
    history = {
        "stories": [{"story_id": source["story_id"], "ts": NOW.timestamp(), "data": {
            "sources": sources, "unresolved_questions": [], "last_decision_id": 1,
        }} for source in sources],
        "delivered": [delivered],
    }
    bundle = stories.evidence_bundle(entries, history, now=NOW, max_age_hours=36)
    payload = stories._model_input(bundle, history)
    assert stories._messages(stories.SYSTEM, payload)
    source_quotes = [(index, source["excerpt"][:500])
                     for index, source in enumerate(bundle, start=1)]
    decision = stories.parse_decision(
        json.dumps(_decision(
            "combine", [source["story_id"] for source in bundle], source_quotes,
        )), bundle, {"stories": [], "delivered": []},
    )
    projected = stories._decision_projection(decision)
    assert stories._messages(stories.REVIEW, {
        "decision": projected, "sources": bundle,
        "confirmed_sent": stories._delivered_view(history),
    })
    assert stories._messages(stories.REVISION, {
        "input": payload, "rejected_decision": projected, "review": _review(decision.record()),
    })


def test_related_history_does_not_match_only_on_url_protocol():
    exact = {"stories": [], "delivered": []}
    recent = {"stories": [{
        "story_id": "other", "data": {"sources": [{
            "url": "https://other.example/beta", "excerpt": "Separate beta material",
        }]},
    }], "delivered": []}
    current = [{"url": "https://current.example/alpha", "excerpt": "Unique alpha signal"}]
    assert stories.related_history(current, exact, recent) == exact


async def test_hold_pointer_does_not_retain_unrelated_batch_source(tmp_path):
    store = await Store(str(tmp_path / "roger.db")).open()
    try:
        sources = stories.prepare_sources([
            _entry("a", SYNTH_FIRST, production=True),
            _entry("b", SYNTH_SECOND, production=True),
        ], now=NOW, max_age_hours=36)
        decision = _decision("hold", [sources[0]["story_id"]], [], reason="No material change.")
        await store.record_curated_story_decision(
            stories.parse_decision(
                json.dumps(decision), sources, {"stories": [], "delivered": []},
            ).record() | {"event_key": None},
            sources, now=NOW.timestamp(),
        )
        context = await store.curated_story_context([sources[0]["story_id"]])
        assert [s["source_id"] for s in context["stories"][0]["data"]["sources"]] == [
            sources[0]["source_id"]
        ]
    finally:
        await store.close()


async def test_persisted_new_url_alias_survives_newer_unrelated_reports(tmp_path):
    store = await Store(str(tmp_path / "roger.db")).open()
    try:
        original = stories.prepare_sources(
            [_entry("original", SYNTH_FIRST, url="https://example.test/original",
                    production=True)],
            now=NOW, max_age_hours=36,
        )
        original_id = original[0]["story_id"]
        first = stories.parse_decision(
            json.dumps(_decision("publish", [original_id], [(1, SYNTH_FIRST)])),
            original, {"stories": [], "delivered": []},
        )
        first_record = first.record() | {"event_key": stories.event_key(first, original)}
        first_id = await store.record_curated_story_decision(
            first_record, original, now=NOW.timestamp(),
        )
        generation = await store.claim_curated_check(NOW.timestamp())
        delivery = await store.claim_curated(
            "2026-10-01", "scout:fixtures", "original",
            event_key=first_record["event_key"], max_posts=20, spacing_seconds=0,
            generation=generation, now=NOW.timestamp(), story_decision_id=first_id,
        )
        await store.mark_curated_sent(delivery, 1)

        first_history = await store.curated_story_context([original_id])
        followup_entry = _entry(
            "followup", SYNTH_SECOND, url="https://example.test/followup", production=True,
        )
        bundle = stories.evidence_bundle(
            [followup_entry], first_history, now=NOW, max_age_hours=36,
        )
        followup_id = stories.story_id(followup_entry)
        update = stories.parse_decision(
            json.dumps(_decision("update", [original_id], [(1, SYNTH_SECOND)])),
            bundle, first_history,
        )
        update_record = update.record() | {"event_key": stories.event_key(update, bundle)}
        update_id = await store.record_curated_story_decision(
            update_record, bundle, now=NOW.timestamp() + 1,
        )
        uncertain_context = await store.curated_story_context([followup_id])
        assert [item["id"] for item in uncertain_context["delivered"]] == [first_id]
        delivery = await store.claim_curated(
            "2026-10-01", "scout:fixtures", "followup",
            event_key=update_record["event_key"], max_posts=20, spacing_seconds=0,
            generation=generation, now=NOW.timestamp() + 1, story_decision_id=update_id,
        )
        await store.mark_curated_sent(delivery, 2)

        for index in range(5):
            source = stories.prepare_sources(
                [_entry(f"other-{index}", SYNTH_FIRST, production=True)],
                now=NOW, max_age_hours=36,
            )
            publish = stories.parse_decision(
                json.dumps(_decision(
                    "publish", [source[0]["story_id"]], [(1, SYNTH_FIRST)],
                )), source, {"stories": [], "delivered": []},
            )
            record = publish.record() | {"event_key": stories.event_key(publish, source)}
            decision_id = await store.record_curated_story_decision(
                record, source, now=NOW.timestamp() + index + 2,
            )
            delivery = await store.claim_curated(
                "2026-10-01", "scout:fixtures", f"other-{index}",
                event_key=record["event_key"], max_posts=20, spacing_seconds=0,
                generation=generation, now=NOW.timestamp() + index + 2,
                story_decision_id=decision_id,
            )
            await store.mark_curated_sent(delivery, index + 3)

        context = await store.curated_story_context([followup_id])
        assert [item["story_id"] for item in context["stories"]] == [original_id]
        assert [item["id"] for item in context["delivered"]] == [update_id, first_id]
    finally:
        await store.close()


async def test_unused_candidate_is_not_a_confirmed_story_alias(tmp_path):
    store = await Store(str(tmp_path / "roger.db")).open()
    try:
        sources = stories.prepare_sources([
            _entry("selected", SYNTH_FIRST, production=True),
            _entry("unused", SYNTH_SECOND, production=True),
        ], now=NOW, max_age_hours=36)
        publish = stories.parse_decision(
            json.dumps(_decision(
                "publish", [sources[0]["story_id"]], [(1, SYNTH_FIRST)],
            )), sources, {"stories": [], "delivered": []},
        )
        record = publish.record() | {"event_key": stories.event_key(publish, sources)}
        decision_id = await store.record_curated_story_decision(
            record, sources, now=NOW.timestamp(),
        )
        generation = await store.claim_curated_check(NOW.timestamp())
        delivery = await store.claim_curated(
            "2026-10-01", "scout:fixtures", "selected", event_key=record["event_key"],
            max_posts=5, spacing_seconds=0, generation=generation, now=NOW.timestamp(),
            story_decision_id=decision_id,
        )
        await store.mark_curated_sent(delivery, 1)

        context = await store.curated_story_context([sources[1]["story_id"]])
        assert context == {"stories": [], "delivered": []}
    finally:
        await store.close()


async def test_pending_story_ids_survive_decision_pruning_and_block_changed_retry(tmp_path):
    store = await Store(str(tmp_path / "roger.db")).open()
    try:
        first_sources = stories.prepare_sources(
            [_entry("a", SYNTH_FIRST, production=True)], now=NOW, max_age_hours=36,
        )
        sid = first_sources[0]["story_id"]
        publish = stories.parse_decision(
            json.dumps(_decision("publish", [sid], [(1, SYNTH_FIRST)])),
            first_sources, {"stories": [], "delivered": []},
        )
        publish_record = publish.record() | {"event_key": stories.event_key(publish, first_sources)}
        publish_id = await store.record_curated_story_decision(
            publish_record, first_sources, now=NOW.timestamp(),
        )
        generation = await store.claim_curated_check(NOW.timestamp())
        delivered = await store.claim_curated(
            "2026-10-01", "scout:fixtures", "a", event_key=publish_record["event_key"],
            max_posts=5, spacing_seconds=0, generation=generation, now=NOW.timestamp(),
            story_decision_id=publish_id,
        )
        await store.mark_curated_sent(delivered, 1, message_url="https://discord.com/channels/1/2/1")

        history = await store.curated_story_context([sid])
        changed_sources = stories.prepare_sources(
            [_entry("a-v2", SYNTH_SECOND, url="https://example.test/a", production=True)],
            now=NOW, max_age_hours=36,
        )
        update = stories.parse_decision(
            json.dumps(_decision("update", [sid], [(1, SYNTH_SECOND)])),
            changed_sources, history,
        )
        update_record = update.record() | {"event_key": stories.event_key(update, changed_sources)}
        update_id = await store.record_curated_story_decision(
            update_record, changed_sources, now=NOW.timestamp() + 1,
        )
        pending = await store.claim_curated(
            "2026-10-01", "scout:fixtures", "a-v2", event_key=update_record["event_key"],
            max_posts=5, spacing_seconds=0, generation=generation, now=NOW.timestamp() + 1,
            story_decision_id=update_id,
        )
        assert pending is not None
        await store._conn.execute("DELETE FROM curated_story_decision WHERE id = ?", (update_id,))
        await store._conn.commit()

        third_sources = stories.prepare_sources([
            _entry("a-v3", SYNTH_SECOND + " changed wording", url="https://example.test/a",
                   production=True),
            _entry("unrelated", SYNTH_FIRST, production=True),
        ], now=NOW, max_age_hours=36)
        combine = stories.parse_decision(
            json.dumps(_decision(
                "combine", [sid, third_sources[1]["story_id"]],
                [(1, SYNTH_SECOND), (2, SYNTH_FIRST)],
            )), third_sources, history,
        )
        combine_record = combine.record() | {"event_key": stories.event_key(combine, third_sources)}
        combine_id = await store.record_curated_story_decision(
            combine_record, third_sources, now=NOW.timestamp() + 2,
        )
        assert await store.claim_curated(
            "2026-10-01", "scout:fixtures", "unrelated",
            event_key=combine_record["event_key"], max_posts=5, spacing_seconds=0,
            generation=generation, now=NOW.timestamp() + 2, story_decision_id=combine_id,
        ) is None
    finally:
        await store.close()


async def test_initial_combine_posts_and_renders_every_cited_source(tmp_path, monkeypatch):
    entries = [
        _entry("a", SYNTH_FIRST, production=True),
        _entry("b", SYNTH_SECOND, production=True),
    ]

    async def collect(*args, **kwargs):
        return ScoutBatch(entries, "fixture-run", 1.0, "")

    monkeypatch.setattr("roger.brains.curated.collect_from_scout", collect)
    ids = [stories.story_id(entry) for entry in entries]
    decision = _decision(
        "combine", ids, [(1, SYNTH_FIRST), (2, SYNTH_SECOND)],
        headline="Two distinct synthetic events share one report",
        fact="Each event keeps its own cited source.",
    )
    llm = FakeLLM(decision)

    class Channel:
        def __init__(self):
            self.sent = []

        async def send(self, *, embed, allowed_mentions):
            self.sent.append(embed)
            return SimpleNamespace(id=10)

    channel = Channel()
    settings = SimpleNamespace(
        curated_channel_id=42, curated_developing_stories=True, guild_id=7, tz="UTC",
        curated_max_posts_per_day=2, curated_min_spacing_minutes=0,
        curated_max_observations_per_day=8, curated_models=["draft"],
        curated_review_models=["review"], scout_digest_path=tmp_path,
        scout_max_age_hours=36,
    )
    store = await Store(str(tmp_path / "roger.db")).open()
    try:
        result = await run_curated_job(
            client=SimpleNamespace(get_channel=lambda _: channel), settings=settings,
            llm=llm, store=store, now=NOW,
        )
        assert result["status"] == "posted"
        source_field = next(field for field in channel.sent[0].fields if field.name == "Sources")
        assert source_field.value == "<https://example.test/a>\n<https://example.test/b>"
    finally:
        await store.close()


async def test_story_publish_cannot_bypass_legacy_seen_or_pending_item(tmp_path):
    store = await Store(str(tmp_path / "roger.db")).open()
    try:
        source = stories.prepare_sources(
            [_entry("legacy", SYNTH_FIRST, production=True)], now=NOW, max_age_hours=36,
        )
        decision = stories.parse_decision(
            json.dumps(_decision("publish", [source[0]["story_id"]], [(1, SYNTH_FIRST)])),
            source, {"stories": [], "delivered": []},
        )
        record = decision.record() | {"event_key": stories.event_key(decision, source)}
        decision_id = await store.record_curated_story_decision(
            record, source, now=NOW.timestamp(),
        )
        generation = await store.claim_curated_check(NOW.timestamp())
        await store.mark_seen([("scout:fixtures", "legacy")])
        assert await store.claim_curated(
            "2026-10-01", "scout:fixtures", "legacy", event_key=record["event_key"],
            max_posts=5, spacing_seconds=0, generation=generation, now=NOW.timestamp(),
            story_decision_id=decision_id,
        ) is None

        legacy_pending = await store.claim_curated(
            "2026-10-01", "scout:fixtures", "pending", event_key="legacy-pending",
            max_posts=5, spacing_seconds=0, generation=generation, now=NOW.timestamp(),
        )
        assert legacy_pending is not None
        pending_source = stories.prepare_sources(
            [_entry("pending", SYNTH_SECOND, production=True)], now=NOW, max_age_hours=36,
        )
        pending_decision = stories.parse_decision(
            json.dumps(_decision(
                "publish", [pending_source[0]["story_id"]], [(1, SYNTH_SECOND)]
            )), pending_source, {"stories": [], "delivered": []},
        )
        pending_record = pending_decision.record() | {
            "event_key": stories.event_key(pending_decision, pending_source)
        }
        pending_id = await store.record_curated_story_decision(
            pending_record, pending_source, now=NOW.timestamp() + 1,
        )
        assert await store.claim_curated(
            "2026-10-01", "scout:fixtures", "pending",
            event_key=pending_record["event_key"], max_posts=5, spacing_seconds=0,
            generation=generation, now=NOW.timestamp() + 1, story_decision_id=pending_id,
        ) is None
    finally:
        await store.close()


async def test_publish_cannot_repeat_sent_story_after_decision_is_pruned(tmp_path):
    store = await Store(str(tmp_path / "roger.db")).open()
    try:
        original = _entry(
            "native-a", SYNTH_FIRST, url="https://example.test/canonical", production=True,
        )
        source = stories.prepare_sources([original], now=NOW, max_age_hours=36)
        publish = stories.parse_decision(
            json.dumps(_decision(
                "publish", [source[0]["story_id"]], [(1, SYNTH_FIRST)],
            )), source, {"stories": [], "delivered": []},
        )
        record = publish.record() | {"event_key": stories.event_key(publish, source)}
        decision_id = await store.record_curated_story_decision(
            record, source, now=NOW.timestamp(),
        )
        generation = await store.claim_curated_check(NOW.timestamp())
        delivery = await store.claim_curated(
            "2026-10-01", "scout:first", "native-a", event_key=record["event_key"],
            max_posts=5, spacing_seconds=0, generation=generation, now=NOW.timestamp(),
            story_decision_id=decision_id,
        )
        await store.mark_curated_sent(delivery, 1)
        await store._conn.execute(
            "DELETE FROM curated_story_decision WHERE id = ?", (decision_id,),
        )
        await store._conn.commit()

        mirror = {
            **_entry(
                "native-b", SYNTH_SECOND, url="https://example.test/canonical", production=True,
            ),
            "feed_url": "scout:mirror",
        }
        changed = stories.prepare_sources([mirror], now=NOW, max_age_hours=36)
        assert changed[0]["story_id"] == source[0]["story_id"]
        retry = stories.parse_decision(
            json.dumps(_decision(
                "publish", [changed[0]["story_id"]], [(1, SYNTH_SECOND)],
            )), changed, {"stories": [], "delivered": []},
        )
        retry_record = retry.record() | {"event_key": stories.event_key(retry, changed)}
        retry_id = await store.record_curated_story_decision(
            retry_record, changed, now=NOW.timestamp() + 1,
        )
        assert await store.claim_curated(
            "2026-10-01", "scout:mirror", "native-b",
            event_key=retry_record["event_key"], max_posts=5, spacing_seconds=0,
            generation=generation, now=NOW.timestamp() + 1, story_decision_id=retry_id,
        ) is None
    finally:
        await store.close()
