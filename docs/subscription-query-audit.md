# Subscription query audit

`pensieve.subscriptions.followed()` means `unfollowed_at IS NULL`; paused and Quiet are independent.
The lifecycle service owns follow, pause, resume, unfollow and restore, with transactional event history.
Locks serialize fetching against lifecycle changes, subscription creation against overlapping imports,
and review actions against other review actions. User ownership checks remain required in every path.

The original `Feed.user_id` sites have these policies:

| Consumers | Policy |
| --- | --- |
| `ai/common.user_feed_ids`, feed maps in `ai/__main__`, `cluster`, `insights`, `memory`, `paper` | Followed only; controls the AI item pools, paper and digest source signals. Saved-link lookups remain special containers. |
| `ai/cluster.cluster_members`, new-item and file-feed jobs | Followed only, including queued work. Existing story history is retained; reader counts use currently followed members. |
| `fetch/jobs.fetch_feed`, `subscribe.refresh_feed` | Lock and re-read state; skip paused or unfollowed feeds before fetching. |
| `fetch/scheduler`, `fetch/favicon` | Existing `paused = false` scheduling excludes unfollowed feeds; Unfollow always pauses. Retention explicitly handles unfollowed rows. |
| `fetch/subscribe` existing-URL and redirect lookups | Include retained rows to restore identity and avoid duplicate URLs. URL uniqueness remains across all lifecycle states. |
| `fetch/opml` existing-URL lookup | Include retained rows to restore; export includes followed rows only. |
| `web/auth_routes.landing` | Followed RSS subscriptions determine whether to show feed setup. |
| `web/reader` lists, folders, tags and navigation | Followed only. Unread total excludes Quiet. Own-feed counts include Quiet. Starred and Saved preserve retained items. |
| `web/reader` story membership/icons | Display counts and icons come from followed feeds; retained canonical articles do not hide followed representatives. |
| `web/queries` item ownership and state updates | Include retained rows so stars, notes, article access and Undo remain usable. Copy propagation targets followed RSS feeds. |
| `web/items` cluster source list | Followed sources only. Article ownership remains retained. |
| `web/manage` feed list, rules feed selector, OPML export | Followed RSS only. Unfollowed has its own Restore section. Resume all selects only followed error-paused feeds. |
| `web/manage` item read-rate statistics, folder reorder/move, rule application, full JSON export | Include retained rows intentionally: these are historical data, ownership or organizational operations, not subscription lists. Rules require an explicit action. |
| `web/search`, `web/insights` feed selectors/title maps | Include retained rows so historical search, notes and stored insight references remain identifiable. |
| `web/favicons`, `web/saved`, `archive/save` | Include retained rows for saved-work rendering and authorization; Saved is a permanent special container. |
| `syncapi/greader.subscription_list` | Followed only. Folder rename/disable organizational queries include retained rows intentionally. |
| `syncapi/common.feed_by_ref`, unread counts, reading-list/kept-unread/folder streams | Followed only. Quiet is normal in third-party clients. |
| `syncapi/common` retained item-ID ownership, visible item base, starred/read/tag history | Include retained items; writes remain user scoped. This preserves third-party access to saved work. |
| `syncapi/fever` groups, feed list, favicon list, fetch timestamps and feed-level mark-read lookup | Followed only. Unread-ID export also excludes unfollowed rows; starred IDs retain saved work. |
| `ai/jobs` user-ID lookup for ledger | Include retained rows to identify the owner of queued work; work itself checks lifecycle state. |

Later consumers (`streams`, `review`, discovery and review routes) query followed feeds for current coverage,
retained rows for lifecycle/Undo, and `stream_follows` for explicit catalog membership. They introduce no
catalog tables, daily-statistics tables, telemetry or autonomous subscription changes.
