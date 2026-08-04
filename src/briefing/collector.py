"""Collector phase: reconcile subscriptions.json against the vendored MCP
server's subscription table, then run check_feeds to pull new content
(including auto-transcribing new YouTube videos).

Acquires the cross-process MCP lock with retry+backoff (unlike the
dashboard's live research jobs, which fail fast) — a daily cron run should
tolerate a brief dashboard research job holding the lock rather than
skipping collection outright. See mcp_client.mcp_lock and
.omc/plans/2026-07-22-control-panel.md "Cross-process lock".
"""

import asyncio
import json
import os
import re
from datetime import datetime

from . import config, db, mcp_client

# check_feeds' per-source result lines, e.g.:
#   "  PyTorch Blog: 0 new items"
#   "  Reddit r/MachineLearning: ERROR — HTTP Error 429: Too Many Requests"
# Leading whitespace is required so the summary line ("Total: N new items
# across M sources", unindented) never matches. Pinned against real
# briefing.log excerpts — see tests/test_collector.py.
_FEED_LINE_RE = re.compile(r"^[ \t]+(.+?):\s+(?:(\d+) new items|ERROR\s+—)", re.MULTILINE)


def log(msg: str):
    from datetime import datetime
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


async def _reconcile(session, conn) -> int:
    """Subscribe to any source in subscriptions.json not yet in the DB.
    Returns the number of new subscriptions added.

    Matches by (source_type, name), not (source_type, identifier) — the
    vendored server rewrites `identifier` for some types after a network
    lookup (youtube: @handle -> resolved channel_id), so matching on the
    original identifier from subscriptions.json would never hit and every
    run would re-attempt (and re-pay the resolution/network cost, or worse,
    a timeout) for already-subscribed sources. Every entry in
    subscriptions.json carries an explicit `name`, so this is reliable."""
    desired = config.load_subscriptions()
    existing = db.existing_subscription_names(conn)

    added = 0
    for sub in desired:
        source_type = sub["source_type"]
        identifier = sub["identifier"]
        name = sub.get("name", "")
        if not sub.get("enabled", True):
            log(f"  SKIP (disabled) '{name or identifier}'")
            continue
        if source_type not in config.KNOWN_SOURCE_TYPES:
            log(f"  SKIP invalid source_type '{source_type}' for '{identifier}' in subscriptions.json")
            continue
        if not name:
            # The (source_type, name) reconcile key collapses for entries
            # without a name — after the first ('news', '') subscribes, every
            # later nameless news entry would look already-present and be
            # silently skipped forever. Fail loudly instead.
            log(f"  SKIP '{identifier}' — missing required 'name' in subscriptions.json")
            continue
        if (source_type, name) in existing:
            continue
        result = await session.call_tool("subscribe", {
            "source_type": source_type,
            "identifier": identifier,
            "name": sub.get("name", ""),
        })
        text = mcp_client.tool_text(result)
        log(f"  subscribed: {source_type}/{identifier} -> {text.strip()[:120]}")
        added += 1
    return added


async def _check_feeds(session) -> str:
    result = await session.call_tool("check_feeds", {})
    return mcp_client.tool_text(result)


def _parse_check_feeds_result(text: str) -> dict[str, bool]:
    """Per-source success/failure from check_feeds' human-readable output.
    There's no structured return from the vendored tool — this is the only
    signal available. See _FEED_LINE_RE for the line shapes matched.

    re.findall returns '' (not None) for a non-participating group, so the
    success check is against a non-empty string, not identity with None."""
    return {
        name.strip(): count != ""
        for name, count in _FEED_LINE_RE.findall(text)
    }


def _update_failure_streaks(results: dict[str, bool]) -> list[str]:
    """Update consecutive_failures per source in subscriptions.json from one
    check_feeds run. Returns names that just crossed the auto-disable
    threshold (caller unsubscribes + disables them).

    A run where EVERY checked source failed is treated as a network-wide
    outage (e.g. the DNS blips seen in production logs, which failed all 23
    sources in the same run) — no counters are touched, and nothing is ever
    disabled for it. Only a source failing while others in the same run
    succeeded counts as that source's own problem."""
    if not results or all(not ok for ok in results.values()):
        return []

    subs = config.load_subscriptions()
    by_name = {s.get("name", ""): s for s in subs}
    newly_disabled = []
    for name, succeeded in results.items():
        sub = by_name.get(name)
        if sub is None:
            continue  # check_feeds reported a source no longer in subscriptions.json
        if succeeded:
            sub["consecutive_failures"] = 0
        else:
            sub["consecutive_failures"] = sub.get("consecutive_failures", 0) + 1
            if sub["consecutive_failures"] >= config.FAILURE_DISABLE_THRESHOLD:
                newly_disabled.append(name)
    config.save_subscriptions(subs)
    return newly_disabled


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "source"


def _export_source_archive(conn, name: str) -> str:
    """Dump a source's stored feed_items to archives/sources/ before it's
    unsubscribed (which cascade-deletes them from feeds.db) — 'Nothing is
    Deleted' (workspace Prime Directive #1). Returns the archive path."""
    rows = conn.execute(
        """SELECT f.* FROM feed_items f
           JOIN subscriptions s ON s.id = f.subscription_id
           WHERE s.name = ?""",
        (name,),
    ).fetchall()
    items = [dict(row) for row in rows]

    archive_dir = os.path.join(config.REPO_ROOT, "archives", "sources")
    os.makedirs(archive_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    path = os.path.join(archive_dir, f"{_slugify(name)}_{stamp}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"source_name": name, "archived_at": datetime.now().isoformat(), "items": items}, f, indent=2)
    return path


async def _disable_source(session, conn, name: str) -> None:
    """Archive a source's stored items, unsubscribe it from the MCP server
    (which also removes those now-archived items from feeds.db), and mark
    it disabled in subscriptions.json so _reconcile never re-subscribes it
    on a later run."""
    subs = config.load_subscriptions()
    sub = next((s for s in subs if s.get("name") == name), None)
    if sub is None:
        log(f"  AUTO-DISABLE '{name}': not found in subscriptions.json, skipping")
        return

    archive_path = _export_source_archive(conn, name)
    result = await session.call_tool("unsubscribe", {
        "source_type": sub["source_type"],
        "identifier": sub["identifier"],
    })
    text = mcp_client.tool_text(result)
    sub["enabled"] = False
    config.save_subscriptions(subs)
    log(
        f"  AUTO-DISABLED '{name}' after {sub.get('consecutive_failures', 0)} "
        f"consecutive failures — archived to {archive_path} -> {text.strip()[:120]}"
    )


async def run_async(run_id: int, conn) -> str:
    """Run the collector phase. Returns 'ok', 'soft_fail', or raises."""
    try:
        with mcp_client.mcp_lock(retry_seconds=120):
            async with mcp_client.McpSession() as session:
                added = await _reconcile(session, conn)
                log(f"Reconciled subscriptions: {added} new")

                check_result = await _check_feeds(session)
                log(f"check_feeds result:\n{check_result}")

                results = _parse_check_feeds_result(check_result)
                newly_disabled = _update_failure_streaks(results)
                for name in newly_disabled:
                    await _disable_source(session, conn, name)

        db.assert_feed_items_schema(conn)
        return "ok"
    except mcp_client.LockHeldError:
        log("SKIPPED collection — dashboard research in progress, proceeding with existing DB content")
        return "soft_fail"


def run(run_id: int, conn) -> str:
    """Sync entrypoint for the CLI."""
    return asyncio.run(run_async(run_id, conn))


if __name__ == "__main__":
    conn = db.connect()
    run_id = db.insert_run(conn, source="cron", started_at=__import__("datetime").datetime.now().isoformat())
    status = run(run_id, conn)
    db.update_run(conn, run_id, collect_status=status)
    conn.close()
