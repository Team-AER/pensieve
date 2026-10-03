import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from pensieve import models
from pensieve.ai import paper
from tests.test_ai_helpers import gateway, make_embedding, make_feed, make_item, make_state  # noqa: F401


async def seed_paper_world(session, user):
    """Three feeds; one cross-source story, several singles, one read item, one hidden-by-rule item."""
    folder = models.Folder(user_id=user.id, name="Tech")
    session.add(folder)
    await session.flush()
    a, b, c = make_feed(user, "Alpha", folder_id=folder.id), make_feed(user, "Beta"), make_feed(user, "Gamma")
    session.add_all([a, b, c])
    await session.flush()
    s1 = make_item(a, "Big launch", "t", age=timedelta(hours=3))
    s2 = make_item(b, "Big launch, covered again", "t", age=timedelta(hours=2))
    s3 = make_item(c, "Big launch (third take)", "t", age=timedelta(hours=1))
    ai1 = make_item(a, "Model release", "t", age=timedelta(hours=4))
    ai2 = make_item(b, "Another model", "t", age=timedelta(hours=5))
    ai3 = make_item(b, "Read already", "t", age=timedelta(hours=6))
    apple = make_item(c, "Phone review", "t", age=timedelta(hours=7))
    untagged = make_item(c, "Mystery post", "t", age=timedelta(hours=8))
    hidden = make_item(c, "Hidden by rule", "t", age=timedelta(hours=9))
    old = make_item(a, "Yesterday's news", "t", age=timedelta(hours=40))
    session.add_all([s1, s2, s3, ai1, ai2, ai3, apple, untagged, hidden, old])
    await session.flush()
    cluster = models.Cluster(
        user_id=user.id, headline="Big launch across the industry", window_start=s1.published_at,
        window_end=s3.published_at, canonical_item_id=s1.id, source_count=3, kind="story",
    )  # fmt: skip
    session.add(cluster)
    await session.flush()
    session.add_all([models.ClusterItem(cluster_id=cluster.id, item_id=i.id) for i in (s1, s2, s3)])
    session.add_all(
        [
            models.ItemAI(user_id=user.id, item_id=s1.id, tags=["ai", "business"], confidences={"ai": 0.9, "business": 0.6}),
            models.ItemAI(user_id=user.id, item_id=s3.id, tags=["ai"], confidences={"ai": 0.8}, summary="- s3 bullet\n\n**Why this matters to you**\n\nbecause\n"),
            models.ItemAI(user_id=user.id, item_id=ai1.id, tags=["ai"], confidences={"ai": 0.9}),
            models.ItemAI(user_id=user.id, item_id=ai2.id, tags=["ai"], confidences={"ai": 0.7}),
            models.ItemAI(user_id=user.id, item_id=ai3.id, tags=["ai"], confidences={"ai": 0.7}),
            models.ItemAI(user_id=user.id, item_id=apple.id, tags=["apple"], confidences={"apple": 0.95}),
            models.ItemAI(user_id=user.id, item_id=old.id, tags=["ai"], confidences={"ai": 0.9}),
        ]
    )  # fmt: skip
    session.add(make_state(user, ai3, read=True))
    session.add(models.ItemState(user_id=user.id, item_id=hidden.id, hidden=True))
    await session.commit()
    return {"a": a, "b": b, "c": c, "cluster": cluster, "s1": s1, "s3": s3, "ai1": ai1, "ai2": ai2, "ai3": ai3,
            "apple": apple, "untagged": untagged, "hidden": hidden, "old": old, "folder": folder}  # fmt: skip


def section_map(body):
    return {s["key"]: s for s in body["sections"]}


async def test_compile_groups_stories_into_tag_sections(session, user):
    w = await seed_paper_world(session, user)
    user.settings = {
        "paper": {"front_page": 0, "min_section": 1}
    }  # sections only; the front page has its own tests
    body = await paper.compile_paper(session, user, paper.today())
    secs = section_map(body)
    # sections in auto (alphabetical) order, "other" last; yesterday's, hidden-by-rule items are out
    assert list(secs) == ["ai", "apple", "other"]
    assert body["item_count"] == 8 and body["story_count"] == 6 and body["window"]["hours"] == 24
    ai = secs["ai"]
    assert ai["title"] == "AI" and ai["count"] == 4 and ai["unread"] == 3
    story = ai["stories"][0]  # same interest everywhere in AI: the extra sources put it first
    assert story["key"] == f"c:{w['cluster'].id}" and story["sources"] == 3 and story["copies"] == 3
    assert story["title"] == "Big launch across the industry" and story["item_id"] == str(w["s1"].id)
    assert story["members"][0]["item_id"] == str(w["s3"].id)  # newest copy first
    assert story["summary"].startswith("- s3") and story["summary_item_id"] == str(w["s3"].id)
    assert story["tags"][0] == "ai" and {f["title"] for f in story["feeds"]} == {"Alpha", "Beta", "Gamma"}
    assert [s["read"] for s in ai["stories"]].count(True) == 1
    assert (
        secs["other"]["stories"][0]["item_id"] == str(w["untagged"].id)
        and secs["other"]["title"] == "Everything else"
    )
    assert all(not s["folded"] for s in ai["stories"]) and ai["brief"] == []


async def test_compile_honours_layout_config(session, user):
    w = await seed_paper_world(session, user)
    user.settings = {
        "paper": {
            "sections": [{"key": "apple", "on": True}, {"key": "ai", "on": True, "limit": 1}],
            "front_page": 0,
            "min_section": 1,
            "min_sources": 2,
            "hide_read": True,
            "auto_sections": False,
        }
    }
    body = await paper.compile_paper(session, user, paper.today())
    secs = section_map(body)
    assert list(secs) == ["apple", "ai", "other"]  # configured order, untagged and unconfigured go to other
    ai = secs["ai"]
    assert [s["key"] for s in ai["stories"]] == [f"c:{w['cluster'].id}"]  # only the 3-source story qualifies
    assert {s["item_id"] for s in ai["brief"]} == {str(w["ai1"].id), str(w["ai2"].id)}
    assert all(not s["read"] for s in ai["brief"])  # hide_read dropped the read single (ai3)
    # per-section limit folds beyond the first story
    user.settings = {
        "paper": {"front_page": 0, "min_section": 1, "sections": [{"key": "ai", "on": True, "limit": 1}]}
    }
    body = await paper.compile_paper(session, user, paper.today())
    ai = section_map(body)["ai"]
    assert [s["folded"] for s in ai["stories"]] == [False, True, True, True]
    # a disabled section disappears; a story with another tag moves there, single-tag ones drop out
    user.settings = {"paper": {"front_page": 0, "min_section": 1, "sections": [{"key": "ai", "on": False}]}}
    body = await paper.compile_paper(session, user, paper.today())
    secs = section_map(body)
    assert list(secs) == ["apple", "business", "other"]
    assert [s["key"] for s in secs["business"]["stories"]] == [f"c:{w['cluster'].id}"]
    # folders as sections: the story sits with its canonical item's feed (Alpha, in Tech)
    user.settings = {"paper": {"front_page": 0, "min_section": 1, "group_by": "folder"}}
    body = await paper.compile_paper(session, user, paper.today())
    secs = section_map(body)
    assert list(secs) == ["tech", "other"] and secs["tech"]["kind"] == "folder"
    assert {s["item_id"] for s in secs["tech"]["stories"]} == {str(w["s1"].id), str(w["ai1"].id)}


async def test_compile_folder_sections_window_and_muted(session, user):
    w = await seed_paper_world(session, user)
    user.settings = {"paper": {"front_page": 0, "min_section": 1, "group_by": "folder", "window_hours": 48}}
    body = await paper.compile_paper(session, user, paper.today())
    secs = section_map(body)
    assert secs["tech"]["title"] == "Tech" and secs["tech"]["kind"] == "folder"
    assert {s["item_id"] for s in secs["tech"]["stories"]} >= {
        str(w["ai1"].id),
        str(w["old"].id),
    }  # 48h window
    assert body["window"]["hours"] == 48
    user.settings = {"paper": {"front_page": 0, "min_section": 1, "muted_feeds": [str(w["c"].id)]}}
    body = await paper.compile_paper(session, user, paper.today())
    assert "apple" not in section_map(body) and body["item_count"] == 5
    story = section_map(body)["ai"]["stories"][0]
    assert story["sources"] == 2 and story["summary"] is None  # Gamma's copy (the summarised one) is muted


async def test_daily_paper_stores_edition_and_keeps_prunes(session, user):
    w = await seed_paper_world(session, user)
    row = await paper.daily_paper(session, user, paper.today())
    await session.commit()
    assert row.kind == "paper" and row.period == paper.today().isoformat() and row.item_refs
    row.body = dict(row.body, hidden=[f"c:{w['cluster'].id}"])
    await session.commit()
    again = await paper.daily_paper(session, user, paper.today())
    await session.commit()
    assert again.id == row.id and again.body["hidden"] == [f"c:{w['cluster'].id}"]
    assert all(s["key"] != f"c:{w['cluster'].id}" for sec in again.body["sections"] for s in sec["stories"])
    rows = (await session.scalars(select(models.Insight).where(models.Insight.user_id == user.id))).all()
    assert len(rows) == 1


def test_paper_config_normalises_and_reads_forms():
    cfg = paper.paper_config(
        models.User(
            settings={
                "paper": {
                    "window_hours": "50",
                    "per_section": 999,
                    "sections": [{"key": "AI", "limit": "3"}, {"key": "ai"}, "junk"],
                }
            }
        )
    )
    assert (
        cfg["window_hours"] == 48 and cfg["per_section"] == paper.MAX_PER_SECTION and cfg["min_sources"] == 1
    )
    assert cfg["sections"] == [{"key": "ai", "on": True, "limit": 3}] and cfg["group_by"] == "tag"
    assert paper.paper_config(None)["show_summaries"] is True

    class Form(dict):
        def getlist(self, key):
            v = self.get(key, [])
            return v if isinstance(v, list) else [v]

    form = Form(group_by="folder", window_hours="72", per_section="5", min_sources="2", hide_read="1",
                section=["apple", "ai"], section_on=["ai"], **{"limit:ai": "4", "limit:apple": ""})  # fmt: skip
    form["muted_feed"] = ["not-a-uuid", "11111111-1111-1111-1111-111111111111"]
    out = paper.config_from_form(form, paper.paper_config(None))
    assert out["group_by"] == "folder" and out["window_hours"] == 72 and out["per_section"] == 5
    assert out["hide_read"] is True and out["show_summaries"] is False and out["auto_sections"] is False
    assert out["sections"] == [
        {"key": "apple", "on": False, "limit": None},
        {"key": "ai", "on": True, "limit": 4},
    ]
    assert out["muted_feeds"] == ["11111111-1111-1111-1111-111111111111"]
    assert (
        paper.section_title("data-engineering") == "Data Engineering" and paper.section_title("ios") == "iOS"
    )


async def test_tuning_ranks_briefs_and_orders_auto_sections(session, user):
    w = await seed_paper_world(session, user)
    # "less" of the story's second tag, Alpha and Gamma, "more" of Beta: the Beta singles now outrank the
    # 3-source story (the second one despite its feed already being listed), and the Alpha single drops to
    # "In brief"
    feeds = {str(w["a"].id): -1, str(w["b"].id): 1, str(w["c"].id): -1}
    user.settings = {
        "paper": {"front_page": 0, "min_section": 1, "tuning": {"tags": {"business": -3}, "feeds": feeds}}
    }
    body = await paper.compile_paper(session, user, paper.today())
    ai = section_map(body)["ai"]
    story = next(s for s in ai["stories"] if s["key"] == f"c:{w['cluster'].id}")
    # business carries 0.61 of the story's 1.72 tag weight: -3 * 0.355 + mean(-1, 1, -1)
    assert story["boost"] == -1.4 and story["sources"] == 3
    assert [s["item_id"] for s in ai["stories"]][:2] == [
        str(w["ai2"].id),
        str(w["ai3"].id),
    ]  # newest first on a tie
    assert ai["stories"][2]["key"] == f"c:{w['cluster'].id}" and [s["item_id"] for s in ai["brief"]] == [
        str(w["ai1"].id)
    ]
    # with min_sources=2 the story's effective 0.44 sources send it to "In brief"; the boosted singles stay out
    feeds = {str(w["a"].id): -3, str(w["b"].id): 1.5, str(w["c"].id): -3}
    user.settings = {
        "paper": {
            "front_page": 0,
            "min_section": 1,
            "min_sources": 2,
            "tuning": {"tags": {"business": -3}, "feeds": feeds},
        }
    }
    body = await paper.compile_paper(session, user, paper.today())
    ai = section_map(body)["ai"]
    assert f"c:{w['cluster'].id}" in {s["key"] for s in ai["brief"]}
    assert {s["item_id"] for s in ai["stories"]} == {str(w["ai2"].id), str(w["ai3"].id)}
    # a positive tag weight orders the automatic sections: apple before ai
    user.settings = {"paper": {"front_page": 0, "min_section": 1, "tuning": {"tags": {"apple": 1}}}}
    body = await paper.compile_paper(session, user, paper.today())
    assert list(section_map(body)) == ["apple", "ai", "other"]
    assert section_map(body)["apple"]["stories"][0]["boost"] == 1.0


def test_tuning_config_apply_and_summary():
    story = {"title": "t", "item_id": "x", "tag": "ai", "tags": ["ai", "business"],
             "tag_weight": {"ai": 0.9, "business": 0.3},
             "feeds": [{"id": "11111111-1111-1111-1111-111111111111", "title": "Alpha"}]}  # fmt: skip
    cfg = paper.paper_config(
        models.User(
            settings={
                "paper": {
                    "tuning": {
                        "tags": {"AI ": "2.5", "x": "nan", "y": 0},
                        "feeds": {"bad": 1, "11111111-1111-1111-1111-111111111111": 9},
                    }
                }
            }
        )
    )
    assert cfg["tuning"] == {"tags": {"ai": 2.5}, "feeds": {"11111111-1111-1111-1111-111111111111": 3.0}}
    assert paper.story_boost(story, cfg["tuning"]) == 5.5
    more = paper.apply_tune(cfg, story, "more")
    assert (
        more["tuning"]["tags"]["ai"] == 3.0
        and more["tuning"]["feeds"]["11111111-1111-1111-1111-111111111111"] == 3.0
    )
    less = paper.apply_tune(paper.paper_config(None), story, "less")
    assert less["tuning"] == {"tags": {"ai": -1.0}, "feeds": {"11111111-1111-1111-1111-111111111111": -0.5}}
    assert paper.tune_summary(less, story) == "AI -1 · Alpha -0.5"
    assert paper.apply_tune(less, story, "reset")["tuning"] == {"tags": {}, "feeds": {}}
    assert paper.tune_summary(paper.paper_config(None), story) == ""
    with pytest.raises(ValueError):
        paper.apply_tune(cfg, story, "sideways")

    class Form(dict):
        def getlist(self, key):
            v = self.get(key, [])
            return v if isinstance(v, list) else [v]

    form = Form(**{"tune_tag:ai": "-1.5", "tune_feed:11111111-1111-1111-1111-111111111111": "0"})
    out = paper.config_from_form(form, cfg)
    assert out["tuning"] == {"tags": {"ai": -1.5}, "feeds": {}}
    assert paper.config_from_form(Form(reset_tuning="1"), cfg)["tuning"] == {"tags": {}, "feeds": {}}


async def test_without_read_pulls_folded_stories_up(monkeypatch):
    ids = [uuid.uuid4() for _ in range(6)]
    stories = [
        {"key": f"s{n}", "read": False, "folded": n >= 3, "members": [{"item_id": str(i)}]}
        for n, i in enumerate(ids)
    ]
    body = {"story_count": 6, "sections": [{"key": "ai", "stories": stories, "brief": []}]}

    async def read_ids(session, user_id, item_ids):
        return set(ids[:2])  # two of the three visible stories were read

    monkeypatch.setattr(paper, "_read_ids", read_ids)
    out = await paper.without_read(None, uuid.uuid4(), body)
    kept = out["sections"][0]["stories"]
    assert [s["key"] for s in kept] == ["s2", "s3", "s4", "s5"]
    # still three shown, one behind "Show more"; the stored edition is untouched
    assert [s["folded"] for s in kept] == [False, False, False, True]
    assert [s["folded"] for s in body["sections"][0]["stories"]] == [False, False, False, True, True, True]


async def seed_volume_world(session, user):
    """A busy feed (Bulk: six AI posts), a quiet one (Quiet: one AI post) and Side (one Apple post). The reader
    opened one older post at angle 0, so a story's interest is the cosine of its embedding's angle: Bulk's
    oldest post is the closest and its newest the furthest, the opposite of recency."""
    bulk, quiet, side, hist = (make_feed(user, n) for n in ("Bulk", "Quiet", "Side", "History"))
    session.add_all([bulk, quiet, side, hist])
    await session.flush()
    seen = make_item(hist, "Read last week", "t", age=timedelta(days=4))
    b = [make_item(bulk, f"Bulk {n}", "t", age=timedelta(hours=6 - n)) for n in range(6)]
    q0 = make_item(quiet, "Quiet essay", "t", age=timedelta(hours=7))
    s0 = make_item(side, "Side phone", "t", age=timedelta(hours=8))
    session.add_all([seen, *b, q0, s0])
    await session.flush()
    angles = dict(zip([*b, q0, s0], [0.1, 0.2, 0.3, 0.4, 1.2, 1.3, 0.5, 1.0], strict=True))
    session.add_all([make_embedding(seen, 0.0), *(make_embedding(i, a) for i, a in angles.items())])
    session.add_all(
        [models.ItemAI(user_id=user.id, item_id=i.id, tags=["ai"], confidences={"ai": 0.8}) for i in [*b, q0]]
    )
    session.add(models.ItemAI(user_id=user.id, item_id=s0.id, tags=["apple"], confidences={"apple": 0.8}))
    session.add(make_state(user, seen, read=True))
    await session.commit()
    return {"bulk": bulk, "quiet": quiet, "side": side, "b": b, "q0": q0, "s0": s0}


def titles(stories):
    return [s["title"] for s in stories]


async def test_sections_rank_by_interest_and_spread_a_busy_feed(session, user):
    await seed_volume_world(session, user)
    user.settings = {"paper": {"front_page": 0, "min_section": 1, "per_section": 3}}
    body = await paper.compile_paper(session, user, paper.today())
    ai = section_map(body)["ai"]
    # interest, not recency: Bulk 0 is Bulk's oldest post. Each further Bulk post loses a step, so the quiet
    # essay (interest rank five of eight) climbs past Bulk 3
    assert titles(ai["stories"]) == [
        "Bulk 0",
        "Bulk 1",
        "Bulk 2",
        "Quiet essay",
        "Bulk 3",
        "Bulk 4",
        "Bulk 5",
    ]
    assert [s["folded"] for s in ai["stories"]] == [False] * 3 + [True] * 4
    first = ai["stories"][0]
    assert first["interest"] > 0.99 and first["score"] == 1.0 and ai["stories"][-1]["score"] == 0.0


async def test_front_page_takes_the_best_unread_with_at_most_two_per_source(session, user):
    w = await seed_volume_world(session, user)
    user.settings = {"paper": {"front_page": 3, "min_section": 1}}
    body = await paper.compile_paper(session, user, paper.today())
    secs = section_map(body)
    assert next(iter(secs)) == paper.FRONT_KEY and secs[paper.FRONT_KEY]["kind"] == "front"
    # Bulk 2 outscores the essay but Bulk already has two
    assert titles(secs[paper.FRONT_KEY]["stories"]) == ["Bulk 0", "Bulk 1", "Quiet essay"]
    assert secs[paper.FRONT_KEY]["unread"] == 3
    # moved, not copied: the sections below hold the rest
    assert titles(secs["ai"]["stories"]) == ["Bulk 2", "Bulk 3", "Bulk 4", "Bulk 5"]
    assert titles(secs["apple"]["stories"]) == ["Side phone"] and body["story_count"] == 8
    # a read story never makes the front page; the next best takes its place
    session.add(make_state(user, w["b"][0], read=True, read_at=w["b"][0].published_at))
    await session.commit()
    body = await paper.compile_paper(session, user, paper.today())
    assert titles(section_map(body)[paper.FRONT_KEY]["stories"]) == ["Bulk 1", "Bulk 2", "Quiet essay"]
    assert titles(section_map(body)["ai"]["stories"])[0] == "Bulk 0"  # still in its section, read
    # 0 turns it off
    user.settings = {"paper": {"front_page": 0, "min_section": 1}}
    body = await paper.compile_paper(session, user, paper.today())
    assert paper.FRONT_KEY not in section_map(body)


async def test_daily_limits_hold_back_a_busy_feed_and_always_show_what_they_keep(session, user):
    w = await seed_volume_world(session, user)
    bulk, quiet = str(w["bulk"].id), str(w["quiet"].id)
    user.settings = {
        "paper": {"front_page": 0, "min_section": 1, "per_section": 1, "feed_limits": {bulk: 2, quiet: 1}}
    }
    body = await paper.compile_paper(session, user, paper.today())
    ai = section_map(body)["ai"]
    # Bulk keeps its two best; they and the essay always show, even past the section's one visible row
    assert titles(ai["stories"]) == ["Bulk 0", "Bulk 1", "Quiet essay"]
    assert [(s["kept"], s["folded"]) for s in ai["stories"]] == [(True, False)] * 3
    assert body["held_back"] == [{"feed_id": bulk, "title": "Bulk", "count": 4, "limit": 2}]
    assert body["story_count"] == 4
    # the limit is a day's worth: a 48-hour paper keeps twice as many
    user.settings["paper"]["window_hours"] = 48
    body = await paper.compile_paper(session, user, paper.today())
    assert body["held_back"][0]["count"] == 2


async def test_daily_limits_count_read_stories_first_and_spare_multi_source_ones(session, user):
    w = await seed_paper_world(session, user)
    # Beta's singles: "Another model" (unread) and "Read already". The read one fills Beta's one a day, and the
    # 3-source story that Beta also carries is never held back.
    user.settings = {"paper": {"front_page": 0, "min_section": 1, "feed_limits": {str(w["b"].id): 1}}}
    body = await paper.compile_paper(session, user, paper.today())
    ai = section_map(body)["ai"]
    keys = {s["key"] for s in ai["stories"]}
    assert f"i:{w['ai3'].id}" in keys and f"i:{w['ai2'].id}" not in keys and f"c:{w['cluster'].id}" in keys
    assert body["held_back"][0]["count"] == 1


def test_percentiles_and_spread():
    assert paper._percentiles([]) == [] and paper._percentiles([0.3]) == [0.5]
    assert paper._percentiles([0.2, 0.9, 0.5]) == [0.0, 1.0, 0.5]
    assert paper._percentiles([0.4, 0.4, 0.4]) == [0.5, 0.5, 0.5]
    assert paper._percentiles([0.1, 0.4, 0.4, 0.9]) == [0.0, 0.5, 0.5, 1.0]

    def row(key, feed, score, sources=1):
        return {"key": key, "feed_id": feed, "score": score, "sources": sources}

    rows = [row("a1", "A", 1.0), row("a2", "A", 0.9), row("a3", "A", 0.85), row("b1", "B", 0.85)]
    assert [s["key"] for s in paper._spread(rows)] == ["a1", "b1", "a2", "a3"]
    # a story several sources carry neither pays the step nor makes its feed pay it, but counts for the cap
    rows = [row("c", "A", 1.0, sources=3), row("a1", "A", 0.9), row("b1", "B", 0.85)]
    assert [s["key"] for s in paper._spread(rows)] == ["c", "a1", "b1"]
    assert [s["key"] for s in paper._spread(rows, limit=2, per_source=1)] == ["c", "b1"]


def test_front_page_and_daily_limit_config():
    fid = "11111111-1111-1111-1111-111111111111"
    cfg = paper.paper_config(
        models.User(
            settings={"paper": {"front_page": 99, "feed_limits": {fid: "7", "nope": 3, str(uuid.uuid4()): 0}}}
        )
    )
    assert cfg["front_page"] == paper.MAX_FRONT_PAGE and cfg["feed_limits"] == {fid: 7}
    defaults = paper.paper_config(None)
    assert defaults["front_page"] == 10 and defaults["feed_limits"] == {} and defaults["per_section"] == 4
    assert defaults["min_section"] == 3
    assert paper.paper_config(models.User(settings={"paper": {"feed_limits": {fid: 500}}}))[
        "feed_limits"
    ] == {fid: 50}

    class Form(dict):
        def getlist(self, key):
            v = self.get(key, [])
            return v if isinstance(v, list) else [v]

    other = "22222222-2222-2222-2222-222222222222"
    cfg = paper.paper_config(models.User(settings={"paper": {"feed_limits": {fid: 7, other: 2}}}))
    out = paper.config_from_form(Form(front_page="-3", **{f"feed_limit:{fid}": ""}), cfg)
    assert out["front_page"] == 0 and out["feed_limits"] == {
        other: 2
    }  # blank lifts it; absent feeds keep theirs
    out = paper.config_from_form(Form(front_page="", **{f"feed_limit:{other}": "4"}), cfg)
    assert out["front_page"] == 10 and out["feed_limits"] == {fid: 7, other: 4}


async def test_without_read_never_folds_kept_stories():
    ids = [uuid.uuid4() for _ in range(5)]
    stories = [
        {"key": "k", "read": False, "folded": False, "kept": True, "members": [{"item_id": str(ids[0])}]},
        *[
            {"key": f"s{n}", "read": False, "folded": n >= 2, "members": [{"item_id": str(i)}]}
            for n, i in enumerate(ids[1:], start=1)
        ],
    ]
    body = {"story_count": 5, "sections": [{"key": "ai", "stories": stories, "brief": []}]}

    async def read_ids(session, user_id, item_ids):
        return {ids[1]}

    import pensieve.ai.paper as mod

    original = mod._read_ids
    mod._read_ids = read_ids
    try:
        out = await paper.without_read(None, uuid.uuid4(), body)
    finally:
        mod._read_ids = original
    kept = out["sections"][0]["stories"]
    # one row was shown besides the kept story; reading it pulls s2 up, s3 and s4 stay folded
    assert [(s["key"], s["folded"]) for s in kept] == [
        ("k", False),
        ("s2", False),
        ("s3", True),
        ("s4", True),
    ]


async def test_small_tags_join_everything_else(session, user):
    w = await seed_paper_world(session, user)
    # AI has four stories, Apple one, the untagged post is already in Everything else
    user.settings = {"paper": {"front_page": 0, "min_section": 2}}
    body = await paper.compile_paper(session, user, paper.today())
    secs = section_map(body)
    assert list(secs) == ["ai", "other"]
    assert {s["item_id"] for s in secs["other"]["stories"]} == {str(w["apple"].id), str(w["untagged"].id)}
    # counted after the front page took its picks: the cluster and two AI singles leave AI with one, read, story
    user.settings = {"paper": {"front_page": 3, "min_section": 2}}
    body = await paper.compile_paper(session, user, paper.today())
    assert list(section_map(body)) == [paper.FRONT_KEY, "other"]
    # folders are the reader's own sections: never merged
    user.settings = {"paper": {"front_page": 0, "min_section": 5, "group_by": "folder"}}
    body = await paper.compile_paper(session, user, paper.today())
    assert list(section_map(body)) == ["tech", "other"]
