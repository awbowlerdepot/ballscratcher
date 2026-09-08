-- 032_transcript_fetcher_runs.sql
--
-- Al: "the one step that we cant force to happen is generating video
-- summaries for a given video and that likely is gated by the transcript
-- scraping that happens on the raspberry pi on my desktop. i can't
-- remember how frequently that wakes up to attempt to get those. can you
-- check on that and then maybe suggest how we can expose that process in
-- the ui so we know when it might happen and how to get the summaries
-- done for article generation."
--
-- Investigation found: scripts/home_transcript_fetcher.py (plain-HTTP)
-- and scripts/home_transcript_fetcher_browser.py (Playwright, the one
-- actually confirmed working -- see that script's own module docstring)
-- both run on the Pi via a `0 7 * * *` cron, once a day, and both share
-- home_transcript_fetcher.py's run() for the admin-API listing/
-- submission/filtering logic (DEPLOY_RUNBOOK.md 6j/6k). Nothing in this
-- project ever recorded WHEN that cron last actually fired or what it
-- found -- run() logs a "Done: {...}" summary locally on the Pi, but
-- that log never reaches AWS/admin-spa. This table is where run() now
-- POSTs that same summary (see the new POST /admin/transcript-fetcher-
-- heartbeat route in admin_api) so the Dashboard can show "last Pi run
-- was X ago" instead of Al having to remember or SSH in to check.
--
-- Append-only (one row per run, not an upserted single-row status) --
-- same "keep history, don't overwrite" convention as product_sku_stock_
-- history and product_price_history in this schema. fetcher_name
-- distinguishes the two scripts sharing this table ('plain' vs
-- 'browser') in case the plain-HTTP one is ever revived for some other
-- use; today only 'browser' is expected to actually report in, given
-- 6j's own documented PoToken dead end.
--
-- No FK, no relation to product_videos -- this is a process-level
-- heartbeat (one row per cron invocation), not a per-video record. The
-- Dashboard's own "videos awaiting transcript" count (get_dashboard_
-- summary) is computed directly from product_videos at read time
-- instead, since that number needs to always be current, not a snapshot
-- from the last run.

begin;

create table transcript_fetcher_runs (
    id uuid primary key default uuid_generate_v4(),
    fetcher_name text not null,
    ran_at timestamptz not null default now(),
    total integer not null,
    got_transcript integer not null,
    no_captions integer not null,
    errors integer not null
);

create index transcript_fetcher_runs_ran_at_idx on transcript_fetcher_runs (ran_at desc);

comment on table transcript_fetcher_runs is 'Heartbeat log for the Pi-side home transcript fetcher (scripts/home_transcript_fetcher.py / home_transcript_fetcher_browser.py, DEPLOY_RUNBOOK.md 6j/6k), which runs off AWS once a day via cron and cannot otherwise be observed from admin-spa. One row per run, POSTed by run() itself via POST /admin/transcript-fetcher-heartbeat right after it finishes. get_dashboard_summary reads the most recent row to show "last Pi run" on the Dashboard (migration 032, Al: "how we can expose that process in the ui so we know when it might happen").';
comment on column transcript_fetcher_runs.fetcher_name is '''plain'' (home_transcript_fetcher.py) or ''browser'' (home_transcript_fetcher_browser.py) -- see DEPLOY_RUNBOOK.md 6j/6k for why the browser-based one is the one actually confirmed working (the plain-HTTP one hits YouTube''s PoToken wall on the content fetch).';
comment on column transcript_fetcher_runs.total is 'How many approved-but-untried candidates run() found needing a transcript on this invocation (needs_transcript() in home_transcript_fetcher.py) -- 0 is a normal, healthy result meaning the backlog was already empty, not a failure.';

commit;
