# Using Pensieve

Pensieve brings RSS subscriptions, saved web pages and optional model-assisted reading into one
self-hosted household reader. Start with the [installation instructions](../README.md#quick-start-with-docker-compose).

## Product landing page

The [public product page](https://aer.app/pensieve/) introduces the paper, memory, sync/save and self-hosting
workflows. Its illustrated desktop/mobile reader uses sample content. The daily paper ranks stories by
similarity to what you open and star, groups multiple sources, and offers a **Safe to skip** action for
low-interest/repeated coverage. Those features depend on your reading history and configured AI processing;
the illustration's counts and scores are examples.

## Welcome and first sign-in

The sign-in and setup screens use Pensieve's mark and orbital artwork alongside a focused account form.
A new server redirects to `/setup`, where the first account becomes the administrator. That account lands
on **Manage → Feeds**. Afterward, the admin can create household accounts; readers sign in using the account
the admin provided. An account with no RSS subscriptions also lands on the feed setup page after sign-in.

Add a feed or site URL, or use **Import OPML** to bring subscriptions from another reader. Folders organize
subscriptions, while rules can automate item handling. Administrators manage household users; each reader
has their own subscriptions and reading state.

## Read and find articles

The reader has navigation, item-list and article panes, with mobile pane switching and keyboard shortcuts.
Use **All unread**, **All items**, **Starred**, folders and tags to narrow the list. The reading menu adjusts
font, text size, line height, width and alignment. Reader mode extracts article text, and **Search** finds
stored content. Notes and stars keep useful items close at hand.

Pensieve includes a PWA manifest and service worker for installation and an offline app shell. Saved-page
archives retain content on your server; this does not promise that every feed article is available in the
browser while disconnected.

## Save pages for later

Use **Save** in the app, or configure the bookmarklet and phone sharing under **Manage → Saving and archive**.
The [browser extension](../extension/README.md) can submit the current tab's rendered HTML when enabled.
API clients can save with `POST /api/v1/save` and a bearer token created in Pensieve.

Saved links have their own **Saved** view. With the capture worker and S3 storage configured, Pensieve keeps
article text, a static page copy, images, a full-page screenshot and original HTML. Starred feed items use
the archive too. Without S3 credentials, saving retains article text only. Pocket, Instapaper and bookmark
exports can be imported in the background.

## Optional AI and Insights

Configure an OpenAI-compatible gateway and model choices under **Manage → AI and memory**. Local inference
keeps model processing on your network only when the configured gateway and its backing models run there.
Feed fetching and page capture still access source websites.

Model-backed features include categorization, story grouping, article summaries, reader memory and
analytics insights. **Insights** also hosts the daily paper: sectioned stories with duplicate coverage
grouped into one row. The paper is compiled from stored results without a fresh model call; earlier
categorization, grouping and summaries can require inference. Feedback and notes help personalize it.

**Ask** (an article's toolbar, a paper story, or the `c` key) opens a chat about that article. Answers come
from the article first; when it leaves something out and **Web** is on, the model plans a few searches, the
bundled SearXNG sends them to public search engines, and the pages found are read and cited as numbered
sources. Each article keeps its conversation; **New chat** clears it. Turn Web off to keep a question to the
article, or turn chat off under **Manage → AI and memory**.

Set `PENSIEVE_AI_ENABLED=false` to use the core reader without model processing. See
[configuration](../README.md#configuration) for gateway, timezone and concurrency settings.

## Use another reading client

Pensieve exposes Google Reader and Fever-compatible sync APIs for clients such as Reeder and NetNewsWire.
Create credentials under **Manage → API tokens** and follow the client’s custom-server configuration.
Tokens also support programmatic saving; keep them private.

## Unfollow, pause and restore

**Unfollow** stops polling and removes a feed from the reader's subscription lists, unread counts,
paper, clustering, OPML export and sync subscription lists. It keeps the feed identity, reading state,
notes and saved-page archives. Search and Starred continue to show retained articles. Restore it from
**Manage → Feeds → Unfollowed**, or add its URL or import it again, to resume the same subscription.

**Pause** keeps a subscription visible but stops fetching. **Resume feeds paused by errors** only
resumes feeds stopped after repeated fetch failures; it leaves user pauses, feeds returning HTTP 410,
unfollowed feeds and the Saved container alone. Individual Resume retries a paused subscription.

Unprotected articles from an unfollowed feed are removed after 30 days from unfollowing. Set
`PENSIEVE_UNFOLLOWED_RETENTION_DAYS` to change that grace period. Starred, noted, cluster-canonical
and insight-cited articles retain the existing protections; saved links are permanent. Once no articles
remain, the unfollowed feed row is removed. Restoring during the grace period cancels this cleanup.
The normal `PENSIEVE_ITEM_RETENTION_DAYS` policy (730 days by default) still applies to older articles.

## Start from curated streams

Open **Manage → Discover** (also offered beside URL entry and OPML import on the first sign-in).
Search streams by topic or language, preview recent articles, and choose the sources you want. Light
sources are checked initially; comprehensive sources are optional. Following puts new subscriptions
in a folder named after the stream. Existing subscriptions keep their folders. An overlapping source
is one feed, with one reading history, even when you follow it through several streams.

A preview never subscribes or creates unread items. It is cached in Redis for an hour by default
(`PENSIEVE_DISCOVERY_CACHE_SECONDS`); expired results are fetched again. Redis and individual source
failures do not prevent browsing the catalog. An unreachable source can still be added through a
stream, with its fetch health reported in Feeds. Rates are measured from dated entries in the sample,
not guaranteed future volume; insufficient dates are shown as **Volume unknown**. The overlap panel
compares up to 40 preview entries with the reader's followed feeds from the last 14 days. Exact URL/hash
matches are measured; title-based story matches are estimates. No detected match is not proof of new
coverage. Both sample sizes are shown.

The bundled catalog starts with Space and science, Open software, and The web platform. It is a
versioned TOML file at `pensieve/streams/catalog.toml`, loaded in memory. Set
`PENSIEVE_STREAM_CATALOG_PATH` to use a local editorial catalog with the same schema. Each source has
a URL, a rationale and light/comprehensive membership. Validate and record current rates with:

```sh
python -m pensieve.streams check --output stream-measurements.json
```

The command uses the guarded fetcher, records measurement time and sample size, and exits nonzero when
any source cannot be validated. The bundled `pensieve/streams/measurements.json` records the starter
catalog's checks. Measurements become stale; rerun the command when maintaining the catalog.

A newer catalog version shows added and retired sources on the stream page. Review the checkboxes
and submit to apply changes. Retired sources are kept unless explicitly selected for unfollowing.
Unchecking a current source stops following it through this stream; it is unfollowed only when no
other stream or direct follow owns it. **Unfollow this stream** opens a checklist. Only feeds exclusive
to streams are preselected; directly followed feeds and feeds in another stream are kept by default.
Nothing is retired, added or unfollowed merely because the catalog changes.

## Review your feed volume

**Manage → Feed review** works with AI disabled. It shows each feed's last 30 days: sample size,
stories per day, exact URL/hash copies, estimated shared stories, how often it published first, and
stories found only in that feed, including the ones you opened, starred or noted. Examples link to
articles so you can judge what another source keeps and what you would lose.

Reduction suggestions require at least 20 articles, 21 days followed, at least 80% repeated coverage,
substantial text and a healthy acquisition history spread across the window. An initial import,
breaking-news spike, missing text, fetch outage, sparse history or valuable only-here reading withholds
suggestions. Fetch health is inferred conservatively from current health, pause history, timely item
acquisitions and continuity; the app does not have a complete historical log of every HTTP request.
Low engagement and a large unread count alone never trigger a suggestion. Story-group corrections
are respected, and a cluster does not establish that an article adds nothing.

On Sundays, the existing AI queue checks up to five shared story pairs per shortlisted feed for added
reporting, analysis, local detail or disagreement. The results and example references are stored as a
`feed_review` insight. Failed or disabled AI is labeled **not checked**. Estimated overlap alone then
withholds reduction suggestions; measured exact copies can still support a Quiet suggestion. Added
angles and uncertainty protect the source. Suggestions are recomputed against current subscriptions.
A page opened before a subscription or reading-state change must be reloaded before applying an action.

Choose one action at a time:

- **Keep · essential** protects the feed. Removing the flag is explicit; Keep still suppresses suggestions
  for 90 days.
- **Limit in paper** uses the paper's existing per-feed daily limit; it does not mark anything read.
- **Quiet** keeps fetching and remains in its own view, All items and the paper, while leaving All unread
  and its total badge. **Normal delivery** reverses it. Third-party clients show Quiet feeds normally.
- **Pause** stops fetching; **Unfollow** uses the retained-data lifecycle described above.
- **Rules and filters** links to existing keyword filtering for topic-level changes.

Recent review changes have durable **Undo** controls. Undo restores the latest change to each retained
feed, including its paper limit. A newer change must be dealt with first. Unfollowed feeds containing
protected items retain their history; an empty feed eventually pruned by retention cannot be restored.

Set a target stories/day, snooze reminders, or turn reminders off in Review preferences. There are no
prompts during the first two weeks after review is first visited (including the Feeds page). Afterward,
a reminder can appear at most weekly, through the weekly review or a Feeds chip, only when evidence
supports a suggestion and observed volume exceeds the target. The page stays available while snoozed
or disabled. Feeds followed in the last three weeks have a **New-follow check-in** with evidence and
the usual actions; there is no automatic removal or trial expiry.

## Discover further

Discovery lists domains cited by at least three articles you opened or starred in the last 60 days,
excluding domains already followed. Repeated links from one article count once. No domain is crawled
until you click its Preview link. Empty reading history falls back to the starter catalog.

Use **Explore sources** on an article or in a folder's list menu for catalog sources sharing its topic.
Topic matching uses catalog membership, folder names, article titles and any available tags, and each
result says why it is shown. When no topic can be established, browse the catalog directly. AI is optional.

**Not interested** hides a catalog source from discovery suggestions. View and reset those choices at
**Discover → View and reset sources marked Not interested**. Dismissals change no subscriptions, article
state, or unread counts. You can still deliberately open that source or choose it on a stream page.
