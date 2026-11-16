"""Conservative starting thresholds. Engagement alone is never a removal signal."""

from datetime import timedelta

MIN_ITEMS = 20
MIN_FOLLOW_DAYS = 21
REPEATED_SHARE = 0.8


def shortlisted(ev):
    return (
        ev.size >= MIN_ITEMS
        and ev.healthy
        and ev.followed_since is not None
        and ev.followed_since <= ev.end - timedelta(days=MIN_FOLLOW_DAYS)
        and ev.repeated_share >= REPEATED_SHARE
        and ev.available_shared / ev.size >= REPEATED_SHARE
        and not ev.feed.essential
        and ev.feed.delivery != "quiet"
        and not ev.recently_kept
        and not (ev.opened_only_here or ev.starred_only_here or ev.noted_only_here)
        and sum(len(it.full_text) >= 120 for it in ev.items) >= ev.size * 0.9
    )


def recommend(ev, angle=None):
    if not shortlisted(ev):
        return None
    angle = angle or {}
    labels = angle.get("labels", [])
    # Added reporting and disagreement protect the feed even when many stories are shared.
    if any(label != "same_content" for label in labels):
        return None
    if ev.exact_share < REPEATED_SHARE and (not labels or len(labels) < len(ev.examples)):
        return None
    if ev.exact_share >= 0.95 and not ev.only_here and labels:
        return "unfollow"
    return (None if ev.paper_limited else "limit") if ev.only_here else "quiet"
