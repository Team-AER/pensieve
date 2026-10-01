from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select

from pensieve import models
from pensieve.web import insights as insights_mod
from tests.test_web_support import HX, login, memory_limiter, seed_feed, seed_item  # noqa: F401


async def test_insights_renders_stored_daily_digest(client, session, user):
    feed = await seed_feed(session, user, "Feed")
    top = await seed_item(session, feed, "Kernel 7.0 lands")
    skip1 = await seed_item(session, feed, "Repost one")
    skip2 = await seed_item(session, feed, "Repost two")
    starred = await seed_item(session, feed, "Starred but unread")
    insight = models.Insight(
        user_id=user.id,
        kind="daily",
        period="2026-09-17",
        title="A quiet Thursday with one big kernel release",
        body={
            "summary": "Mostly kernel news; the rest can wait.",
            "top_stories": [
                {
                    "cluster_id": None,
                    "item_id": str(top.id),
                    "title": top.title,
                    "why": "You read every kernel release note.",
                    "sources": 4,
                    "tag": "linux",
                    "affinity": 0.92,
                }
            ],
            "safe_to_skip": {
                "count": 2,
                "story_count": 1,
                "item_ids": [str(skip1.id), str(skip2.id)],
                "reason": "Reposts of a story you already read.",
            },
            "starred_unread": [{"item_id": str(starred.id), "title": starred.title}],
            "trends": [{"name": "Rust in kernel", "series": [1, 2, 2, 5, 8, 7, 9], "delta": 6}],
        },
    )
    session.add(insight)
    await session.commit()
    headers = await login(client, user)
    r = await client.get("/insights/digest")
    assert r.status_code == 200
    assert "Thursday, 17 September" in r.text
    assert "Kernel 7.0 lands" in r.text and "You read every kernel release note." in r.text
    assert "4 sources" in r.text and "92% match" in r.text
    assert "Rust in kernel" in r.text and "<polyline" in r.text and "+6" in r.text
    assert "2 items" in r.text and "Mark these read" in r.text
    assert "Starred but unread" in r.text
    await session.refresh(insight)
    assert insight.opened_at is not None
    # Mark safe-to-skip items read.
    r = await client.post(f"/insights/{insight.id}/skip-read", headers=headers | HX)
    assert r.status_code == 200 and "Marked 2 items as read" in r.text
    for iid in (skip1.id, skip2.id):
        state = await session.get(models.ItemState, (user.id, iid))
        assert state is not None and state.is_read
    assert await session.get(models.ItemState, (user.id, top.id)) is None
    r = await client.get(f"/insights/{insight.id}")
    assert r.status_code == 200 and "Marked 2 items as read" in r.text


async def test_insights_empty_state_enqueues_digest(client, session, user, monkeypatch):
    calls = []

    async def enqueue(function, *args, **kwargs):
        calls.append((function, args))

    monkeypatch.setattr(insights_mod.queue, "enqueue", enqueue)
    headers = await login(client, user)
    r = await client.get("/insights/digest")
    assert r.status_code == 200 and "No digest yet." in r.text and "/insights/generate" in r.text
    r = await client.post("/insights/generate", data={"kind": "daily"}, headers=headers)
    assert r.status_code == 303 and r.headers["location"] == "/insights/digest?queued=1"
    assert calls == [("ai_daily_digest", (str(user.id),))]
    r = await client.get("/insights/weekly")
    assert r.status_code == 200 and "No review yet." in r.text


async def test_insights_scoped_by_user(client, session, user):
    from tests.test_web_support import make_user

    other = await make_user(session)
    theirs = models.Insight(user_id=other.id, kind="daily", period="2026-09-18", title="Not yours", body={})
    session.add(theirs)
    await session.commit()
    await login(client, user)
    r = await client.get("/insights/digest")
    assert "Not yours" not in r.text
    r = await client.get(f"/insights/{theirs.id}")
    assert r.status_code == 404


async def seed_paper(session, user):
    a, b = await seed_feed(session, user, "Alpha"), await seed_feed(session, user, "Beta")
    s1 = await seed_item(session, a, "Big launch")
    s2 = await seed_item(session, b, "Big launch again")
    single = await seed_item(session, a, "Lone AI post")
    phone = await seed_item(session, b, "Phone review")
    cluster = models.Cluster(
        user_id=user.id,
        headline="Big launch everywhere",
        window_start=s1.published_at,
        window_end=s2.published_at,
        canonical_item_id=s1.id,
        source_count=2,
        kind="story",
    )
    session.add(cluster)
    await session.flush()
    session.add_all(
        [
            models.ClusterItem(cluster_id=cluster.id, item_id=s1.id),
            models.ClusterItem(cluster_id=cluster.id, item_id=s2.id),
        ]
    )
    session.add_all(
        [
            models.ItemAI(
                user_id=user.id,
                item_id=s1.id,
                tags=["ai"],
                confidences={"ai": 0.9},
                summary="- Launch bullet\n- Two\n- Three\n\n**Why this matters to you**\n\nBecause you follow launches.\n",
            ),
            models.ItemAI(user_id=user.id, item_id=single.id, tags=["ai"], confidences={"ai": 0.8}),
            models.ItemAI(user_id=user.id, item_id=phone.id, tags=["apple"], confidences={"apple": 0.9}),
        ]
    )
    session.add(models.Tag(user_id=user.id, name="ai", kind="ai"))
    session.add(models.Tag(user_id=user.id, name="apple", kind="ai"))
    await session.commit()
    return {"a": a, "b": b, "s1": s1, "s2": s2, "single": single, "phone": phone, "cluster": cluster}


async def test_paper_compiles_on_open_and_renders_sections(client, session, user):
    w = await seed_paper(session, user)
    user.settings = {
        "paper": {"front_page": 0, "min_section": 1}
    }  # three stories would all make the front page
    await session.commit()
    headers = await login(client, user)
    r = await client.get("/insights")
    assert r.status_code == 200 and "The paper" in r.text
    assert "3 stories from 4 items" in r.text
    assert r.text.index('id="sec-ai"') < r.text.index('id="sec-apple"')
    assert "Big launch everywhere" in r.text and "2 sources" in r.text
    # the why's last word shares a nowrap span with the rewrite icon
    assert "Launch bullet" in r.text and "Because you follow" in r.text and "launches." in r.text
    assert "Big launch again" in r.text and "Lone AI post" in r.text and "no summary yet" in r.text
    assert 'id="paper-settings"' in r.text and 'name="section" value="ai"' in r.text
    edition = await session.scalar(
        select(models.Insight).where(models.Insight.user_id == user.id, models.Insight.kind == "paper")
    )
    assert edition is not None and edition.opened_at is not None
    # remove the story: gone from the edition (the whole paper comes back) and kept out on recompile
    key = f"c:{w['cluster'].id}"
    r = await client.post(f"/insights/paper/{edition.id}/hide", data={"key": key}, headers=headers | HX)
    assert r.status_code == 200 and "story-removed" in r.headers.get("HX-Trigger", "")
    assert r.headers.get("HX-Retarget") == "#insight" and "Big launch everywhere" not in r.text
    assert "Lone AI post" in r.text and "Remove from paper" in r.text and ">Hide<" not in r.text
    state = await session.get(models.ItemState, (user.id, w["s1"].id))
    assert state is None or not state.is_read  # removed, not read
    r = await client.get("/insights?refresh=1")
    assert "Big launch everywhere" not in r.text and "Lone AI post" in r.text
    # mark a story read through the paper
    r = await client.post(
        f"/insights/paper/{edition.id}/read", data={"key": f"i:{w['single'].id}"}, headers=headers | HX
    )
    assert r.status_code == 200 and "paper-read" in r.headers.get("HX-Trigger", "")
    assert "Lone AI post" not in r.text and 'id="sec-apple"' in r.text  # read: off today's paper
    state = await session.get(models.ItemState, (user.id, w["single"].id))
    assert state is not None and state.is_read
    # and a whole section
    r = await client.post(
        f"/insights/paper/{edition.id}/read", data={"section": "apple"}, headers=headers | HX
    )
    assert r.status_code == 200 and 'id="sec-apple"' not in r.text
    state = await session.get(models.ItemState, (user.id, w["phone"].id))
    assert state is not None and state.is_read


async def test_paper_drops_stories_read_in_reader(client, session, user):
    w = await seed_paper(session, user)
    user.settings = {"paper": {"front_page": 0, "min_section": 1}}
    await session.commit()
    headers = await login(client, user)
    r = await client.get("/insights")
    assert 'id="sec-apple"' in r.text and "3 stories" in r.text
    r = await client.post(f"/items/{w['phone'].id}/read", headers=headers | HX)
    assert r.status_code == 200
    r = await client.get("/insights")
    assert 'id="sec-apple"' not in r.text and "2 stories" in r.text
    # one copy of a two-source story read: the story stays until every copy is
    await client.post(f"/items/{w['s1'].id}/read", headers=headers | HX)
    r = await client.get("/insights")
    assert "Big launch everywhere" in r.text


async def test_paper_read_marks_every_copy_of_the_article(client, session, user):
    w = await seed_paper(session, user)
    old = datetime.now(UTC) - timedelta(days=3)
    c = await seed_feed(session, user, "Gamma")  # the same link and the same text in another feed
    # in the story's cluster but too old for the paper
    older = await seed_item(session, w["a"], "Big launch, first word", published_at=old)
    session.add(models.ClusterItem(cluster_id=w["cluster"].id, item_id=older.id))
    relink = await seed_item(session, c, "Launch link", published_at=old, url=w["s2"].url)
    rehash = await seed_item(session, c, "Big launch", published_at=old)
    rehash.hash = w["s1"].hash
    # a series groups different episodes: reading one reads no other
    episode = await seed_item(session, w["a"], "Launch recap #2", published_at=old)
    series = models.Cluster(
        user_id=user.id, window_start=old, window_end=old, canonical_item_id=episode.id, kind="series"
    )
    saved = models.Feed(
        user_id=user.id, url=models.SAVED_FEED_URL, title="Saved", kind=models.FEED_KIND_SAVED
    )
    session.add_all([series, saved])
    await session.flush()
    session.add_all(
        [
            models.ClusterItem(cluster_id=series.id, item_id=episode.id),
            models.ClusterItem(cluster_id=series.id, item_id=w["s1"].id),
        ]
    )
    await session.commit()
    # saved on purpose: stays in My list
    kept = await seed_item(session, saved, "Saved launch", url=w["s1"].url)
    read = {it.title: it.id for it in (w["s1"], w["s2"], older, relink, rehash)}
    unread = {it.title: it.id for it in (episode, kept, w["single"], w["phone"])}
    headers = await login(client, user)
    r = await client.get("/insights")
    edition = await session.scalar(
        select(models.Insight).where(models.Insight.user_id == user.id, models.Insight.kind == "paper")
    )
    r = await client.post(
        f"/insights/paper/{edition.id}/read", data={"key": f"c:{w['cluster'].id}"}, headers=headers | HX
    )
    assert r.status_code == 200 and "Big launch everywhere" not in r.text
    for title, iid in read.items():
        state = await session.get(models.ItemState, (user.id, iid))
        assert state is not None and state.is_read, title
    for title, iid in unread.items():
        state = await session.get(models.ItemState, (user.id, iid))
        assert state is None or not state.is_read, title


async def test_paper_front_page_daily_limits_and_sources(client, session, user):
    w = await seed_paper(session, user)
    extra = [await seed_item(session, w["a"], f"Alpha extra {n}") for n in range(3)]
    session.add_all(
        [models.ItemAI(user_id=user.id, item_id=i.id, tags=["ai"], confidences={"ai": 0.5}) for i in extra]
    )
    user.settings = {"paper": {"front_page": 2}}
    await session.commit()
    headers = await login(client, user)
    r = await client.get("/insights")
    assert r.status_code == 200
    # the front page comes first (chip and section) and shows the summary's first bullet as a dek
    assert r.text.index('href="#sec-front-page"') < r.text.index('href="#sec-ai"')
    assert r.text.index('id="sec-front-page"') < r.text.index('id="sec-ai"')
    assert "Your best 2 from every section" in r.text and 'class="pstory-dek">Launch bullet<' in r.text
    front = r.text.split('id="sec-front-page"')[1].split("<section")[0]
    assert "Mark front page read" in front and "Turn off section" not in front
    # Customize lists sources busiest first with their volume and an "A day" box
    sources = r.text.split('class="source-rows"')[1]
    assert sources.index("Alpha") < sources.index("Beta") and "5 a week" in sources
    assert f'name="feed_limit:{w["a"].id}"' in sources and 'name="front_page"' in r.text
    edition = await session.scalar(
        select(models.Insight).where(models.Insight.user_id == user.id, models.Insight.kind == "paper")
    )
    r = await client.post(
        f"/insights/paper/{edition.id}/read", data={"section": "front-page"}, headers=headers | HX
    )
    assert r.status_code == 200 and 'id="sec-front-page"' not in r.text
    # a daily limit of 1 on Alpha: one Alpha single stays, the rest are held back and named in the masthead
    r = await client.post(
        "/insights/paper/settings",
        data={"group_by": "tag", "window_hours": "24", "per_section": "4", "min_sources": "1", "front_page": "0",
              "show_summaries": "1", "auto_sections": "1", f"feed_limit:{w['a'].id}": "1",
              f"feed_limit:{w['b'].id}": ""},
        headers=headers,
    )  # fmt: skip
    assert r.status_code == 303
    await session.refresh(user)
    cfg = user.settings["paper"]
    assert cfg["front_page"] == 0 and cfg["feed_limits"] == {str(w["a"].id): 1}
    r = await client.get("/insights")
    # Alpha has four singles: its best one stays (a read one first), three are held back and named in the masthead
    assert "Daily limits held back 3 stories" in r.text and "Alpha 3" in r.text


async def test_paper_settings_reorder_hide_and_mute(client, session, user):
    w = await seed_paper(session, user)
    headers = await login(client, user)
    r = await client.post(
        "/insights/paper/settings",
        data={"group_by": "tag", "window_hours": "48", "per_section": "3", "min_sources": "2",
              "show_summaries": "1", "auto_sections": "1", "section": ["apple", "ai"],
              "section_on": ["apple", "ai"], "limit:ai": "1", "muted_feed": str(w["b"].id)},
        headers=headers,
    )  # fmt: skip
    assert r.status_code == 303 and r.headers["location"] == "/insights?saved=1"
    await session.refresh(user)
    cfg = user.settings["paper"]
    assert cfg["window_hours"] == 48 and cfg["min_sources"] == 2 and cfg["muted_feeds"] == [str(w["b"].id)]
    assert cfg["sections"] == [
        {"key": "apple", "on": True, "limit": None},
        {"key": "ai", "on": True, "limit": 1},
    ]
    r = await client.get("/insights?saved=1")
    assert "Layout saved" in r.text
    # Beta is muted: the story lost a source and drops to "In brief" under min_sources=2; apple is gone entirely
    assert 'id="sec-apple"' not in r.text and "In brief" in r.text and "Big launch again" not in r.text
    r = await client.post("/insights/paper/hide-section", data={"key": "ai"}, headers=headers)
    assert r.status_code == 303
    await session.refresh(user)
    assert {s["key"]: s["on"] for s in user.settings["paper"]["sections"]}["ai"] is False
    r = await client.get("/insights")
    assert 'id="sec-ai"' not in r.text
    r = await client.post("/insights/paper/mute-feed", data={"feed_id": str(w["a"].id)}, headers=headers)
    assert r.status_code == 303
    await session.refresh(user)
    assert set(user.settings["paper"]["muted_feeds"]) == {str(w["a"].id), str(w["b"].id)}


async def test_paper_off_and_other_users_edition(client, session, user):
    from tests.test_web_support import make_user

    other = await make_user(session)
    theirs = models.Insight(
        user_id=other.id, kind="paper", period="2026-09-18", title="Their paper", body={"sections": []}
    )
    session.add(theirs)
    user.settings = {"paper": False}
    await session.commit()
    headers = await login(client, user)
    r = await client.get("/insights")
    assert r.status_code == 200 and "switched off" in r.text
    r = await client.get(f"/insights/{theirs.id}")
    assert r.status_code == 404
    r = await client.post(f"/insights/paper/{theirs.id}/hide", data={"key": "x"}, headers=headers)
    assert r.status_code == 404


async def test_paper_tune_more_less_reset_records_corrections(client, session, user):
    w = await seed_paper(session, user)
    headers = await login(client, user)
    r = await client.get("/insights")
    assert "More like this" in r.text and 'name="tune_tag:' not in r.text and "Nothing tuned yet" in r.text
    edition = await session.scalar(
        select(models.Insight).where(models.Insight.user_id == user.id, models.Insight.kind == "paper")
    )
    key = f"c:{w['cluster'].id}"
    r = await client.post(
        f"/insights/paper/{edition.id}/tune", data={"key": key, "direction": "more"}, headers=headers | HX
    )
    assert r.status_code == 200 and "paper-tuned-more" in r.headers.get("HX-Trigger", "")
    assert 'class="chip chip-sm pstory-boost up"' in r.text and "+1.5" in r.text
    assert " open>" in r.text  # the swapped-in story stays expanded, so the next action is still in reach
    assert "Tuned: AI +1 · Beta +0.5 · Alpha +0.5" in r.text and ">Reset<" in r.text
    await session.refresh(user)
    tuning = user.settings["paper"]["tuning"]
    assert tuning["tags"] == {"ai": 1.0} and tuning["feeds"] == {str(w["a"].id): 0.5, str(w["b"].id): 0.5}
    # the edition was recompiled with the new weights
    await session.refresh(edition)
    story = next(s for sec in edition.body["sections"] for s in sec["stories"] if s["key"] == key)
    assert story["boost"] == 1.5
    r = await client.post(
        f"/insights/paper/{edition.id}/tune", data={"key": key, "direction": "less"}, headers=headers | HX
    )
    assert r.status_code == 200 and "pstory-boost" not in r.text  # back to zero: no chip
    await session.refresh(user)
    assert user.settings["paper"]["tuning"] == {"tags": {}, "feeds": {}}
    r = await client.post(
        f"/insights/paper/{edition.id}/tune", data={"key": key, "direction": "less"}, headers=headers | HX
    )
    assert "pstory-boost down" in r.text and "-1.5" in r.text
    rows = list(
        await session.scalars(
            select(models.Correction)
            .where(models.Correction.user_id == user.id)
            .order_by(models.Correction.created_at)
        )
    )
    assert [c.new_value for c in rows] == [
        "more of: ai, Beta, Alpha",
        "less of: ai, Beta, Alpha",
        "less of: ai, Beta, Alpha",
    ]
    assert (
        rows[0].target_type == "paper_story"
        and rows[0].field == "preference"
        and rows[0].old_value == "Big launch everywhere"
    )
    # the customize panel now lists the weights, and saving it with an edited weight sticks
    r = await client.get("/insights")
    assert 'name="tune_tag:ai"' in r.text and f'name="tune_feed:{w["a"].id}"' in r.text
    r = await client.post(
        "/insights/paper/settings",
        data={"tune_tag:ai": "2", f"tune_feed:{w['a'].id}": "0", "section": ["ai"], "section_on": ["ai"]},
        headers=headers,
    )
    assert r.status_code == 303
    await session.refresh(user)
    assert user.settings["paper"]["tuning"] == {"tags": {"ai": 2.0}, "feeds": {str(w["b"].id): -0.5}}
    r = await client.post(
        f"/insights/paper/{edition.id}/tune", data={"key": key, "direction": "reset"}, headers=headers | HX
    )
    assert r.status_code == 200 and "paper-tuned-reset" in r.headers.get("HX-Trigger", "")
    await session.refresh(user)
    assert user.settings["paper"]["tuning"] == {"tags": {}, "feeds": {}}
    r = await client.post(
        f"/insights/paper/{edition.id}/tune", data={"key": key, "direction": "sideways"}, headers=headers | HX
    )
    assert r.status_code == 400
    r = await client.post(
        f"/insights/paper/{edition.id}/tune",
        data={"key": "i:nope", "direction": "more"},
        headers=headers | HX,
    )
    assert r.status_code == 404


async def test_paper_summary_prefs_rewrite_and_missing(client, session, user, monkeypatch):
    calls = []

    async def enqueue(function, *args, **kwargs):
        calls.append((function, args, kwargs.get("_job_id")))

    monkeypatch.setattr(insights_mod.queue, "enqueue", enqueue)
    w = await seed_paper(session, user)
    headers = await login(client, user)
    r = await client.get("/insights")
    assert (
        "2 without a summary" in r.text
        and "write them now" in r.text
        and 'data-confirm-title="Rewrite this summary?"' in r.text
    )
    edition = await session.scalar(
        select(models.Insight).where(models.Insight.user_id == user.id, models.Insight.kind == "paper")
    )
    # summary preferences ride on the customize form
    r = await client.post(
        "/insights/paper/settings",
        data={
            "bullets": "5",
            "why": "general",
            "focus": " homelab  stuff ",
            "show_summaries": "1",
            "section": ["ai"],
            "section_on": ["ai"],
        },
        headers=headers,
    )
    assert r.status_code == 303
    await session.refresh(user)
    assert user.settings["summaries"] == {"bullets": 5, "why": "general", "focus": "homelab stuff"}
    r = await client.get("/insights")
    assert (
        'value="5" selected' in r.text and 'value="general" selected' in r.text and "homelab stuff" in r.text
    )
    # Rewrite on the story's why: a correction and a rewrite job carrying the dialog's note
    key = f"c:{w['cluster'].id}"
    r = await client.post(
        f"/insights/paper/{edition.id}/rewrite",
        data={"key": key, "note": "no funding talk"},
        headers=headers | HX,
    )
    assert (
        r.status_code == 200
        and "Rewriting" in r.text
        and f"/insights/paper/{edition.id}/summary?key=" in r.text
    )
    assert calls[-1][0] == "ai_summarize_item" and calls[-1][1] == (
        str(user.id),
        str(w["s1"].id),
        "no funding talk",
    )
    corr = await session.scalar(select(models.Correction).where(models.Correction.user_id == user.id))
    assert (
        corr.target_type == "item_summary"
        and corr.field == "why_it_matters"
        and corr.new_value == "no funding talk"
    )
    assert corr.old_value.startswith("- Launch bullet")
    # polling: still the old text -> pending; a new summary -> rendered, edition updated
    r = await client.get(
        f"/insights/paper/{edition.id}/summary", params={"key": key, "n": 1}, headers=headers | HX
    )
    assert r.status_code == 200 and "Rewriting" in r.text and "n=2" in r.text
    ai = await session.get(models.ItemAI, (user.id, w["s1"].id))
    ai.summary = "- New bullet\n\n**Why this matters to you**\n\nBetter why.\n"
    await session.commit()
    r = await client.get(
        f"/insights/paper/{edition.id}/summary", params={"key": key, "n": 2}, headers=headers | HX
    )
    assert "New bullet" in r.text and "Better" in r.text and "why." in r.text and "Rewriting" not in r.text
    await session.refresh(edition)
    story = next(s for sec in edition.body["sections"] for s in sec["stories"] if s["key"] == key)
    assert story["summary"].startswith("- New bullet") and "rewrite_of" not in story
    # a failed job within the last minutes surfaces as an error with a retry
    single_key = f"i:{w['single'].id}"
    r = await client.post(
        f"/insights/paper/{edition.id}/summarize", data={"key": single_key}, headers=headers | HX
    )
    assert (
        r.status_code == 200
        and "Summarizing" in r.text
        and calls[-1][1] == (str(user.id), str(w["single"].id))
    )
    session.add(
        models.AIJob(
            kind="summarize",
            user_id=user.id,
            target_id=w["single"].id,
            status="failed",
            last_error="boom",
            finished_at=datetime.now(UTC),
        )
    )
    await session.commit()
    r = await client.get(
        f"/insights/paper/{edition.id}/summary", params={"key": single_key, "n": 1}, headers=headers | HX
    )
    assert "The summary failed: boom" in r.text and ">Retry" in r.text or "Retry</button>" in r.text
    # after too many polls it gives up politely
    await session.execute(delete(models.AIJob))
    await session.commit()
    r = await client.get(
        f"/insights/paper/{edition.id}/summary", params={"key": single_key, "n": 40}, headers=headers | HX
    )
    assert "taking longer than usual" in r.text
    # "write them now" queues one summaries job for the stories without one
    n_before = len(calls)
    r = await client.post(f"/insights/paper/{edition.id}/summaries", headers=headers)
    assert r.status_code == 303 and r.headers["location"] == f"/insights/{edition.id}?summaries=2"
    assert calls[n_before][0] == "ai_summarize_items" and set(calls[n_before][1][1]) == {
        str(w["single"].id),
        str(w["phone"].id),
    }
    r = await client.get(f"/insights/{edition.id}?summaries=2", headers=headers)
    assert "Queued summaries for 2 stories" in r.text
    r = await client.get(f"/insights/{edition.id}?summaries=0", headers=headers)
    assert "Every story already has a summary." in r.text
    r = await client.get(f"/insights/{edition.id}?summaries=failed", headers=headers)
    assert "Couldn&#39;t reach the AI queue" in r.text or "Couldn't reach the AI queue" in r.text
    # why "off" hides the why paragraph even when the stored summary carries one
    user.settings = dict(user.settings, summaries={"why": "off"})
    await session.commit()
    r = await client.get("/insights?refresh=1")
    assert "New bullet" in r.text and "Better why." not in r.text
