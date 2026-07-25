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

from . import config, db, mcp_client


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


async def run_async(run_id: int, conn) -> str:
    """Run the collector phase. Returns 'ok', 'soft_fail', or raises."""
    try:
        with mcp_client.mcp_lock(retry_seconds=120):
            async with mcp_client.McpSession() as session:
                added = await _reconcile(session, conn)
                log(f"Reconciled subscriptions: {added} new")

                check_result = await _check_feeds(session)
                log(f"check_feeds result:\n{check_result}")

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
