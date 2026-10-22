-- Runbook 6ej: oil pattern catalog (spec docs/partner-api-v1-patterns.md).
--
-- Drilld proposed an oil pattern catalog in the partner API so Flare can
-- roll a ball over a real pattern's oil. Al: "go with your suggestion ...
-- pulling in the patterns and using OCR is fine but they need approved
-- before they are added to the live catalog. similar pattern to the
-- videos. manual uploads of patterns as the first way they get pulled in
-- then we can look for sources later. leave house shots as just another
-- pattern for now."
--
-- One row per pattern. Staff upload a sheet (private S3 bucket, never
-- republished), pattern_ingest reads it with Claude vision into a PENDING
-- row, staff correct it in admin and approve. Only approved + published
-- rows reach the partner API. The derived fields (steps' board numbers,
-- board_totals, profile, ratio) are always written by admin_api's
-- oil_patterns.compute so every partner gets one interpretation.

begin;

create table if not exists oil_patterns (
    id                       uuid primary key default uuid_generate_v4(),
    name                     text not null,
    short_name               text,
    category                 text not null default 'sport'
                             check (category in ('house', 'sport', 'challenge', 'recreational', 'tournament')),
    series                   text,
    source                   text not null default 'upload'
                             check (source in ('kegel', 'pba', 'usbc', 'wtba', 'brunswick', 'center', 'manual', 'upload')),
    source_url               text,
    sheet_url                text,              -- the original publisher's sheet; null for uploads
    image_url                text,              -- the original publisher's graph image; null for uploads
    status                   text not null default 'current' check (status in ('current', 'retired')),

    length_feet              numeric,
    reverse_brush_drop_feet  numeric,
    oil_per_board_ul         numeric,
    forward_ml               numeric,
    reverse_ml               numeric,
    volume_ml                numeric,
    ratio                    numeric,           -- largest track zone ratio (computed)
    track_zones              jsonb not null default '[]'::jsonb,
    machine                  jsonb not null default '{}'::jsonb,
    steps                    jsonb not null default '[]'::jsonb,
    board_totals             jsonb,             -- computed
    profile                  jsonb,             -- computed: µL per board per foot
    profile_source           text not null default 'steps' check (profile_source in ('steps', 'graph')),

    -- Review, like product_videos: pending -> approved (published) or rejected.
    review_status            text not null default 'pending' check (review_status in ('pending', 'approved', 'rejected')),
    published                boolean not null default false,
    review_note              text,
    resolved_at              timestamptz,
    resolved_by              text,

    -- Upload + OCR (private bucket keys; never in the partner API).
    upload_key               text,
    upload_filename          text,
    page_image_key           text,
    extraction_status        text not null default 'none'
                             check (extraction_status in ('none', 'queued', 'running', 'ready', 'failed')),
    extraction_error         text,
    extraction_model         text,
    extracted_at             timestamptz,

    created_at               timestamptz not null default now(),
    created_by               text,
    updated_at               timestamptz not null default now(),
    content_changed_at       timestamptz not null default now()
);

create index if not exists oil_patterns_feed_idx on oil_patterns (content_changed_at, id);
create index if not exists oil_patterns_review_idx on oil_patterns (review_status);

-- The partner feed's clock: moves when anything a partner sees changes
-- (including publish/unpublish), not on review or OCR bookkeeping.
create or replace function oil_patterns_bump_content_changed_at() returns trigger
language plpgsql as $$
begin
  if (new.name, new.short_name, new.category, new.series, new.source, new.source_url, new.sheet_url, new.image_url,
      new.status, new.length_feet, new.reverse_brush_drop_feet, new.oil_per_board_ul, new.forward_ml, new.reverse_ml,
      new.volume_ml, new.ratio, new.track_zones, new.machine, new.steps, new.board_totals, new.profile,
      new.profile_source, new.published)
     is distinct from
     (old.name, old.short_name, old.category, old.series, old.source, old.source_url, old.sheet_url, old.image_url,
      old.status, old.length_feet, old.reverse_brush_drop_feet, old.oil_per_board_ul, old.forward_ml, old.reverse_ml,
      old.volume_ml, old.ratio, old.track_zones, old.machine, old.steps, old.board_totals, old.profile,
      old.profile_source, old.published) then
    new.content_changed_at := now();
  end if;
  new.updated_at := now();
  return new;
end;
$$;

drop trigger if exists oil_patterns_content_changed_at on oil_patterns;
create trigger oil_patterns_content_changed_at
  before update on oil_patterns
  for each row execute function oil_patterns_bump_content_changed_at();

comment on table oil_patterns is
  'Runbook 6ej: oil pattern catalog. Uploaded sheets are read by pattern_ingest (Claude vision) into pending rows; staff review and approve in admin; only review_status=approved AND published rows reach the partner API (/v1/patterns*). Derived fields come from admin_api oil_patterns.compute.';

commit;
