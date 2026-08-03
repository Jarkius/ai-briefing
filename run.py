#!/usr/bin/env python3
"""AI Briefing orchestrator: collect -> research -> generate -> send.

Each phase fails soft — a broken feed, a bot-blocked Google search, or a
lock contention with a concurrent dashboard job logs and lets the pipeline
continue with whatever it has, rather than aborting the whole daily run.

Usage:
    python run.py              # full run, sends email
    python run.py --dry-run    # full run, prints instead of sending
"""

import os
import socket
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from briefing import collector, config, db, generator, researcher, sender  # noqa: E402

NETWORK_WAIT_TIMEOUT_SECONDS = 45
NETWORK_WAIT_POLL_INTERVAL_SECONDS = 3


def log(msg: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def wait_for_network(timeout: int = NETWORK_WAIT_TIMEOUT_SECONDS) -> bool:
    """Poll for real outbound connectivity before starting the pipeline.

    launchd's StartCalendarInterval can fire during macOS DarkWake, where the
    CPU is up but WiFi hasn't reassociated yet — the first provider calls
    fail with connection errors even though the fallback chain recovers a
    few seconds later. Waiting here closes that gap instead of relying on
    every downstream provider's own retry logic to absorb it.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("1.1.1.1", 443), timeout=3):
                return True
        except OSError:
            time.sleep(NETWORK_WAIT_POLL_INTERVAL_SECONDS)
    return False


def main():
    dry_run = "--dry-run" in sys.argv
    config.require_env()

    if wait_for_network():
        log("Network check: ok")
    else:
        log(f"Network check: no connectivity after {NETWORK_WAIT_TIMEOUT_SECONDS}s — proceeding anyway")

    conn = db.connect()
    run_id = db.insert_run(conn, source="cron", started_at=datetime.now().isoformat())

    collect_status = "skipped"
    try:
        log("=== Phase 1: Collect ===")
        collect_status = collector.run(run_id, conn)
    except Exception as e:
        log(f"Collect phase failed (soft): {e}")
        collect_status = f"error: {e}"
    db.update_run(conn, run_id, collect_status=collect_status)

    research_findings = ""
    research_status = "skipped"
    try:
        log("=== Phase 2: Research ===")
        research_findings, count = researcher.run_pending()
        research_status = f"ok ({count} processed)" if count else "ok (nothing pending)"
    except Exception as e:
        log(f"Research phase failed (soft): {e}")
        research_status = f"error: {e}"
    db.update_run(conn, run_id, research_status=research_status)

    result = None
    generate_status = "skipped"
    try:
        log("=== Phase 3: Generate ===")
        result = generator.generate(conn, research_findings=research_findings)
        generate_status = "ok"
    except Exception as e:
        log(f"Generate phase failed: {e}")
        generate_status = f"error: {e}"
    db.update_run(conn, run_id, generate_status=generate_status)

    send_status = "skipped"
    if result:
        if dry_run:
            log("=== Phase 4: Send (dry-run — printing instead) ===")
            print("\n=== PART 1 ===\n", result["part1_html"][:1000])
            print("\n=== PART 2 ===\n", result["part2_html"][:1000])
            send_status = "dry_run"
        else:
            log("=== Phase 4: Send ===")
            try:
                send_result = sender.send_two_part_briefing(
                    result["part1_html"], result["part2_html"], result["date_str"],
                )
                log(f"Send result: {send_result}")
                send_status = str(send_result)
                if result.get("archive_file"):
                    db.record_send_status(result["archive_file"], send_result)
            except Exception as e:
                log(f"Send phase failed: {e}")
                send_status = f"error: {e}"
    db.update_run(conn, run_id, send_status=send_status, finished_at=datetime.now().isoformat())
    conn.close()

    log("=== Done ===")


if __name__ == "__main__":
    main()
