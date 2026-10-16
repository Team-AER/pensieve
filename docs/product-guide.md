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

Set `PENSIEVE_AI_ENABLED=false` to use the core reader without model processing. See
[configuration](../README.md#configuration) for gateway, timezone and concurrency settings.

## Use another reading client

Pensieve exposes Google Reader and Fever-compatible sync APIs for clients such as Reeder and NetNewsWire.
Create credentials under **Manage → API tokens** and follow the client’s custom-server configuration.
Tokens also support programmatic saving; keep them private.
