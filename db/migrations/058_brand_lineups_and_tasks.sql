-- 058_brand_lineups_and_tasks.sql -- brand lineups, standing partner
-- tasks, and the brand-art images they produce (DEPLOY_RUNBOOK.md 6fq).
--
-- Al (2026-11-02, through the Bowler Depot brand-stores session):
-- BowlerIQ should own each brand's lineups, and Bowler Depot's brand-store
-- art is a TASK BowlerIQ reruns on its own when a trigger rule fires, with
-- each image served at a stable URL. "This is one per brand, not a one-off
-- for Storm... adding the next brand should only mean loading its lineups
-- and creating its task." Then: "go ahead, include the weekly sweep, newest
-- releases for heroes."
--
-- Nothing here is Storm-specific: Storm is only the first data loaded
-- (scripts/load_brand_lineups.py).

-- A brand's own ways of grouping its balls (Storm: "Reaction profile" and
-- "Lane read"; Hammer: performance tiers and families). source says where
-- the grouping was read.
create table if not exists brand_groupings (
    id            uuid primary key default uuid_generate_v4(),
    brand_id      uuid not null references brands(id) on delete cascade,
    key           text not null,
    label         text not null,
    source        text,
    display_order integer not null default 0,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    unique (brand_id, key)
);

-- One line inside a grouping ("Strong Symmetrical"). assign_rule decides
-- how a newly released ball gets filed into it, as data, so a new brand
-- needs no code:
--   {"type": "manual"}                         -- admin files it (default)
--   {"type": "brand_listing", "platform": "commercebuild",
--    "url": "https://www.stormbowling.com/...?filter%5Bcustom20%5D%5B0%5D=strong-symmetrical"}
--                                              -- the brand's own filtered listing
--   {"type": "fields", "all": [{"field": "name", "matches": "(?i)^black widow"}]}
--                                              -- a match on our own columns
-- A ball no rule places goes to review_queue (field_name 'brand_line'),
-- never into a guessed line.
create table if not exists brand_lines (
    id            uuid primary key default uuid_generate_v4(),
    grouping_id   uuid not null references brand_groupings(id) on delete cascade,
    key           text not null,
    label         text not null,
    text          text,
    display_order integer not null default 0,
    assign_rule   jsonb not null default '{"type": "manual"}',
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now(),
    unique (grouping_id, key)
);

-- A ball can sit in several lines (a reaction profile AND a lane read).
-- added_by: 'load' (load_brand_lineups.py), 'rule' (assign_rule), 'admin'.
-- note is the line-specific blurb the theme shows ("Medium oil. Balanced
-- midlane read.").
create table if not exists brand_line_balls (
    line_id    uuid not null references brand_lines(id) on delete cascade,
    product_id uuid not null references products(id) on delete cascade,
    position   integer not null default 0,
    note       text,
    added_by   text not null default 'admin' check (added_by in ('load', 'rule', 'admin')),
    added_at   timestamptz not null default now(),
    primary key (line_id, product_id)
);
create index if not exists idx_brand_line_balls_product on brand_line_balls(product_id);

-- A standing request from a partner that BowlerIQ reruns by itself.
-- kind 'brand_art' is the only kind so far. params (brand_art):
--   {"palette": {"background": "#191c1b", "accent": "#009d90"},
--    "slug": "storm", "bigcommerce_brand_id": 42,
--    "line_groupings": ["reaction-profile"],      -- which groupings get line images
--    "heroes": [{"type": "product", "pick": "newest_ball", "rank": 1}, ...]}
-- triggers: the rules that rerun it ('ball_released', 'line_changed',
-- 'photo_changed', 'params_changed', 'companion_stock', 'weekly_sweep',
-- 'manual'). The daily dispatcher compares each slot's inputs with what it
-- was last drawn from, so a rule "fires" when its inputs changed.
create table if not exists tasks (
    id               uuid primary key default uuid_generate_v4(),
    owner            text not null,
    brand_id         uuid not null references brands(id) on delete cascade,
    kind             text not null check (kind in ('brand_art')),
    params           jsonb not null default '{}',
    triggers         jsonb not null default '["ball_released", "line_changed", "photo_changed", "params_changed", "companion_stock", "weekly_sweep", "manual"]',
    enabled          boolean not null default true,
    status           text not null default 'idle' check (status in ('idle', 'queued', 'running', 'failed')),
    last_run_at      timestamptz,
    last_full_run_at timestamptz,
    last_result      jsonb,
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now(),
    unique (owner, brand_id, kind)
);

create table if not exists task_runs (
    id          uuid primary key default uuid_generate_v4(),
    task_id     uuid not null references tasks(id) on delete cascade,
    trigger     text not null,
    slots       text[] not null default '{}',
    status      text not null default 'running' check (status in ('running', 'succeeded', 'partial', 'failed')),
    detail      jsonb,
    started_at  timestamptz not null default now(),
    finished_at timestamptz
);
create index if not exists idx_task_runs_task on task_runs(task_id, started_at desc);

-- One drawn image per slot. The image lives at a fixed key
-- (brand-art/<slug>/<slot_key>.webp, plus -16x9 for heroes) that a rerun
-- overwrites, served by img.bowleriq.io with a one-hour cache. featured is
-- what the image shows, so the partner's headline and link can follow it:
-- [{"product_id", "bigcommerce_product_id", "name", "kind"}].
-- input_hash fingerprints what the slot was drawn from (balls, photos,
-- palette, companion stock); the dispatcher reruns a slot when it differs.
create table if not exists brand_art_slots (
    task_id        uuid not null references tasks(id) on delete cascade,
    slot_key       text not null,
    kind           text not null check (kind in ('line', 'hero')),
    line_id        uuid references brand_lines(id) on delete set null,
    hero_type      text check (hero_type in ('product', 'lineup')),
    featured       jsonb not null default '[]',
    image_key      text,
    image_key_16x9 text,
    width          integer,
    height         integer,
    alt            text,
    method         text,            -- 'gemini' or 'composite' (how it was drawn)
    input_hash     text,
    generated_at   timestamptz,
    error          text,
    primary key (task_id, slot_key)
);
