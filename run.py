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
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from briefing import collector, config, db, generator, researcher, sender  # noqa: E402


def log(msg: str):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def main():
    dry_run = "--dry-run" in sys.argv
    config.require_env()

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
    db.update_run(conn, run_id, send_status=send_status)

    social_post_status = "skipped"
    if result:
        try:
            log("=== Phase 5: Social Post ===")
            candidates = generator.social_post_candidate_items(conn)
            fetched = researcher.deep_fetch_items_sync(candidates)
            if not fetched:
                log("Social post skipped: no fetchable sources (no URLs, or all fetches failed)")
                social_post_status = "skipped (no fetchable sources)"
            else:
                source_material = generator.build_social_post_source(fetched)
                post_text = generator.generate_social_post(source_material, result["date_str"])
                if dry_run:
                    print("\n=== SOCIAL POST ===\n", post_text[:1000])
                    social_post_status = "dry_run"
                else:
                    post_html = sender.render_social_post_html(post_text, result["date_str"])
                    social_post_status = sender.send_social_post_email(post_html, result["date_str"])
                    log(f"Social post send result: {social_post_status}")
        except Exception as e:
            log(f"Social post phase failed (soft): {e}")
            social_post_status = f"error: {e}"
    db.update_run(conn, run_id, social_post_status=social_post_status, finished_at=datetime.now().isoformat())
    conn.close()

    log("=== Done ===")


if __name__ == "__main__":
    main()
