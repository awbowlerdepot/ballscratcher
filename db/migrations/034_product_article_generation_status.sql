-- 034_product_article_generation_status.sql
--
-- Al: "i have noticed that images take forever to get generated. not
-- sure what is causing this but is there a way to show the status in
-- the UI? is it in the queue how many ahead etc."
--
-- Investigation found: there is no literal FIFO queue for an on-demand
-- single-product generate/regenerate -- admin_api.queue_article_
-- generation does a direct, immediate lambda:InvokeFunction (Invocation
-- Type="Event") against ProductArticleGeneratorFunction, same "no queue
-- in front" convention queue_video_discovery/queue_rescrape already
-- use. It starts right away; it's just that ONE invocation legitimately
-- takes a long time -- up to NUM_GEMINI_CANDIDATES_PER_VARIANT (3)
-- candidates x 2 shot variants = 6 sequential/interleaved Gemini image
-- calls, each independently eligible for GEMINI_RETRY_TOTAL (4) retries
-- with exponential backoff (2s/4s/8s/16s) if Vertex AI rate-limits it --
-- see build_gemini_scene_prompt's and get_gemini_requests_session's own
-- docstrings for that history. The function's 600s Lambda timeout
-- exists for exactly this reason. None of that was ever visible to
-- admin-spa: queue_article_generation returns {"queued": true}
-- immediately and the UI has no idea whether/when the invocation
-- actually finishes short of polling generated_at and hoping it moved.
--
-- This table is the fix: a live, single-row-per-in-flight-generation
-- status, upserted by queue_article_generation right when it invokes
-- the Lambda and deleted by product_article_generator's handler() in a
-- try/finally once that invocation finishes (success OR failure/
-- timeout), so a crashed run doesn't leave the UI showing "generating"
-- forever. Deliberately NOT an append-only history table like transcript_
-- fetcher_runs (032) -- that one logs a heartbeat for a process with no
-- other completion signal; this one only needs to answer "is a
-- generation for this product in flight right now, and since when,"
-- which a single upserted-then-deleted row does more simply than
-- history plus a "most recent row has no completed_at yet" query would.
--
-- One row per product_id (not one per article) so a first-time
-- generate-article click -- before any product_articles row exists --
-- is just as visible as a regenerate of an existing article.

begin;

create table product_article_generation_status (
    product_id uuid primary key references products(id) on delete cascade,
    started_at timestamptz not null default now(),
    mode text not null
);

comment on table product_article_generation_status is 'Live "is a generation in flight right now" marker per product (034_product_article_generation_status.sql), NOT a history table -- one row exists only while product_article_generator is actively working on that product_id. Upserted by admin_api.queue_article_generation right before/around its lambda:InvokeFunction call; deleted by product_article_generator/app.py handler()''s on-demand path in a try/finally so a crash or timeout still clears it. Lets the admin-spa Articles/product-detail UI show a real "Generating... (started Xs ago)" indicator instead of the previous "queued: true and then silence" -- Al: "images take forever to get generated... is there a way to show the status in the UI."';
comment on column product_article_generation_status.mode is 'Same mode string queue_article_generation accepts: "both"/"text"/"images"/"action_shot"/"product_shot" -- lets the UI say e.g. "Generating product shot..." rather than a generic "Generating...".';

commit;
