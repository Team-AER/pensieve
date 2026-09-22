
from datetime import timedelta

import pytest
from sqlalchemy import select

from pensieve import models
from pensieve.ai import paper
from tests.test_ai_helpers import gateway, make_feed, make_item, make_state  # noqa: F401


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
    body = await paper.compile_paper(session, user, paper.today())
    secs = section_map(body)
    # sections in auto (alphabetical) order, "other" last; yesterday's, hidden-by-rule items are out
    assert list(secs) == ["ai", "apple", "other"]
    assert body["item_count"] == 8 and body["story_count"] == 6 and body["window"]["hours"] == 24
    ai = secs["ai"]
    assert ai["title"] == "AI" and ai["count"] == 4 and ai["unread"] == 3
    story = ai["stories"][0]  # most sources first
    assert story["key"] == f"c:{w['cluster'].id}" and story["sources"] == 3 and story["copies"] == 3
    assert story["title"] == "Big launch across the industry" and story["item_id"] == str(w["s1"].id)
    assert story["members"][0]["item_id"] == str(w["s3"].id)  # newest copy first
    assert story["summary"].startswith("- s3") and story["summary_item_id"] == str(w["s3"].id)
    assert story["tags"][0] == "ai" and {f["title"] for f in story["feeds"]} == {"Alpha", "Beta", "Gamma"}
    assert [s["read"] for s in ai["stories"]].count(True) == 1
    assert secs["other"]["stories"][0]["item_id"] == str(w["untagged"].id) and secs["other"]["title"] == "Everything else"
    assert all(not s["folded"] for s in ai["stories"]) and ai["brief"] == []


async def test_compile_honours_layout_config(session, user):
    w = await seed_paper_world(session, user)
    user.settings = {
        "paper": {
            "sections": [{"key": "apple", "on": True}, {"key": "ai", "on": True, "limit": 1}],
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
    user.settings = {"paper": {"sections": [{"key": "ai", "on": True, "limit": 1}]}}
    body = await paper.compile_paper(session, user, paper.today())
    ai = section_map(body)["ai"]
    assert [s["folded"] for s in ai["stories"]] == [False, True, True, True]
    # a disabled section disappears; a story with another tag moves there, single-tag ones drop out
    user.settings = {"paper": {"sections": [{"key": "ai", "on": False}]}}
    body = await paper.compile_paper(session, user, paper.today())
    secs = section_map(body)
    assert list(secs) == ["apple", "business", "other"]
    assert [s["key"] for s in secs["business"]["stories"]] == [f"c:{w['cluster'].id}"]
    # folders as sections: the story sits with its canonical item's feed (Alpha, in Tech)
    user.settings = {"paper": {"group_by": "folder"}}
    body = await paper.compile_paper(session, user, paper.today())
    secs = section_map(body)
    assert list(secs) == ["tech", "other"] and secs["tech"]["kind"] == "folder"
    assert {s["item_id"] for s in secs["tech"]["stories"]} == {str(w["s1"].id), str(w["ai1"].id)}


async def test_compile_folder_sections_window_and_muted(session, user):
    w = await seed_paper_world(session, user)
    user.settings = {"paper": {"group_by": "folder", "window_hours": 48}}
    body = await paper.compile_paper(session, user, paper.today())
    secs = section_map(body)
    assert secs["tech"]["title"] == "Tech" and secs["tech"]["kind"] == "folder"
    assert {s["item_id"] for s in secs["tech"]["stories"]} >= {str(w["ai1"].id), str(w["old"].id)}  # 48h window
    assert body["window"]["hours"] == 48
    user.settings = {"paper": {"muted_feeds": [str(w["c"].id)]}}
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
    cfg = paper.paper_config(models.User(settings={"paper": {"window_hours": "50", "per_section": 999, "sections": [{"key": "AI", "limit": "3"}, {"key": "ai"}, "junk"]}}))
    assert cfg["window_hours"] == 48 and cfg["per_section"] == paper.MAX_PER_SECTION and cfg["min_sources"] == 1
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
    assert out["sections"] == [{"key": "apple", "on": False, "limit": None}, {"key": "ai", "on": True, "limit": 4}]
    assert out["muted_feeds"] == ["11111111-1111-1111-1111-111111111111"]
    assert paper.section_title("data-engineering") == "Data Engineering" and paper.section_title("ios") == "iOS"


async def test_tuning_ranks_briefs_and_orders_auto_sections(session, user):
    w = await seed_paper_world(session, user)
    # "less" of the story's second tag, Alpha and Gamma, "more" of Beta: the Beta singles now outrank the
    # 3-source story, and the Alpha single drops to "In brief"
    feeds = {str(w["a"].id): -1, str(w["b"].id): 1, str(w["c"].id): -1}
    user.settings = {"paper": {"tuning": {"tags": {"business": -3}, "feeds": feeds}}}
    body = await paper.compile_paper(session, user, paper.today())
    ai = section_map(body)["ai"]
    story = next(s for s in ai["stories"] if s["key"] == f"c:{w['cluster'].id}")
    # business carries 0.61 of the story's 1.72 tag weight: -3 * 0.355 + mean(-1, 1, -1)
    assert story["boost"] == -1.4 and story["sources"] == 3
    assert [s["item_id"] for s in ai["stories"]][:2] == [str(w["ai2"].id), str(w["ai3"].id)]  # newest first on a tie
    assert ai["stories"][2]["key"] == f"c:{w['cluster'].id}" and [s["item_id"] for s in ai["brief"]] == [str(w["ai1"].id)]
    # with min_sources=2 the story's effective 0.44 sources send it to "In brief"; the boosted singles stay out
    feeds = {str(w["a"].id): -3, str(w["b"].id): 1.5, str(w["c"].id): -3}
    user.settings = {"paper": {"min_sources": 2, "tuning": {"tags": {"business": -3}, "feeds": feeds}}}
    body = await paper.compile_paper(session, user, paper.today())
    ai = section_map(body)["ai"]
    assert f"c:{w['cluster'].id}" in {s["key"] for s in ai["brief"]}
    assert {s["item_id"] for s in ai["stories"]} == {str(w["ai2"].id), str(w["ai3"].id)}
    # a positive tag weight orders the automatic sections: apple before ai
    user.settings = {"paper": {"tuning": {"tags": {"apple": 1}}}}
    body = await paper.compile_paper(session, user, paper.today())
    assert list(section_map(body)) == ["apple", "ai", "other"]
    assert section_map(body)["apple"]["stories"][0]["boost"] == 1.0


def test_tuning_config_apply_and_summary():
    story = {"title": "t", "item_id": "x", "tag": "ai", "tags": ["ai", "business"],
             "tag_weight": {"ai": 0.9, "business": 0.3},
             "feeds": [{"id": "11111111-1111-1111-1111-111111111111", "title": "Alpha"}]}  # fmt: skip
    cfg = paper.paper_config(models.User(settings={"paper": {"tuning": {"tags": {"AI ": "2.5", "x": "nan", "y": 0}, "feeds": {"bad": 1, "11111111-1111-1111-1111-111111111111": 9}}}}))
    assert cfg["tuning"] == {"tags": {"ai": 2.5}, "feeds": {"11111111-1111-1111-1111-111111111111": 3.0}}
    assert paper.story_boost(story, cfg["tuning"]) == 5.5
    more = paper.apply_tune(cfg, story, "more")
    assert more["tuning"]["tags"]["ai"] == 3.0 and more["tuning"]["feeds"]["11111111-1111-1111-1111-111111111111"] == 3.0
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
