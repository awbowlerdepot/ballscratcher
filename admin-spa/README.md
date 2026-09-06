# admin-spa

Phase 1 of the new admin SPA (task #465-473: "a more sophisticated admin
spa... hosted on AWS and has users and an improved UI"). Vite + React +
TypeScript + Tailwind, client-side routed (react-router), talking to
`AdminApiFunction` (the same backend the existing `admin-site/index.html`
vanilla-JS tool uses) -- but authenticated as a real per-user Cognito
account instead of a shared bearer-token secret.

## What's here (phase 1)

- **Sign-in** (`/login`) -- Cognito `USER_SRP_AUTH` via
  `amazon-cognito-identity-js`. Accounts are admin-created only
  (`AllowAdminCreateUserOnly: true` on `AdminUserPool` in
  `template.yaml`) -- there's no public sign-up form here or anywhere
  else.
- **Dashboard** (`/`) -- the landing page, not a tab you click into
  (per Al's "dashboards more prominent" priority). KPI tiles, an
  ADU-by-brand bar chart, and four Top-10 tables, all from
  `GET /admin/dashboard`.
- **Products** (`/products`) -- filters (status, brand id, search,
  source platform, sort, the three "missing X" checkboxes), a sortable
  table, checkbox multi-select, and a bulk "Rescrape selected" action --
  `GET /products` + `POST /products/{id}/rescrape`.
- **Review Queue** (`/review-queue`) -- the scraped-field-correction
  moderation queue (a `field_name` is either a whitelisted product
  column or a per-SKU `rg`/`differential`/`mass_bias` value -- see
  `api/types.ts`'s `ReviewQueueItem` comment). Status/product-id
  filters, a pending-count badge, per-row and bulk approve/reject
  (bulk fires sequentially with a 300ms pause between calls, matching
  `admin-site/index.html`'s own throttling-avoidance precedent), and a
  reason modal for rejects. There's no inline-edit of the proposed
  value on either this or the old UI -- approve always applies the
  scraped value exactly as-is.
- **Video Candidates** (`/video-candidates`) -- YouTube review videos
  matched to products. Status (pending/approved/rejected -- "all"
  exists server-side but isn't exposed here, matching the old tab's own
  dropdown)/product-id filters, pending-count badge, per-row and bulk
  approve/reject (same 300ms-paced sequential bulk pattern), an Undo
  button on non-pending rows (`POST .../restore`, no confirm needed --
  matches the old UI), and a per-row Reassign action (move a video to a
  different product; the origin candidate is tombstoned as rejected).
  No bulk-reassign or delete here -- those stay product-detail-only
  features until Products gets a detail sub-view.
- **Articles** (`/articles`) -- AI-generated ball-review articles
  (`022_product_articles.sql` onward). Status/product-id filters, a
  full-article preview modal (hook/performance summary/who-should-buy-
  or-skip/pros-cons/buying tips/verdict/FAQ, plus comparison-table and
  inferred-siblings counts), per-row approve/reject, a BigCommerce
  sync toggle with "Sync now"/"Resync" triggers, decoupled
  "Regen text"/"Regen images" buttons, and an image-candidate picker
  (`026_product_article_image_candidates.sql`, Gemini vs. Stability
  shots side by side, click to select which one is live). No bulk
  actions and no pending-count badge here -- `admin-site/index.html`'s
  own Articles tab never had either (see `api/types.ts`'s
  `ArticleListItem` comment on the missing pending_count).
- **Price Sites** (`/price-sites`) -- combines admin-site's two separate
  top-level tabs into one page, per Al's own question about whether
  that made sense (checked: nothing about the two backend resources
  conflicts, admin-site just never happened to combine their UI).
  Top section is **Price Sources**, the discovered-match review queue
  (`product_price_sources`) -- status/product-id filters, pending badge,
  per-row and bulk approve/reject (same 300ms-paced sequential bulk
  pattern as Review Queue/Video Candidates), and Undo on resolved rows.
  Bottom section is **Price Sites**, the retailer registry
  (`price_sites`) that Price Sources' `price_site_id` points at -- an
  always-visible add-site form (fetch method toggles between
  scrape-config fields and API-config fields), a list with Edit/
  Deactivate-Reactivate/Delete, and an edit `Modal` prefilled with every
  field the site actually has (fetch_method itself isn't editable --
  switching scrape/api is a delete-and-recreate, matching admin-site's
  own `editPriceSite` reasoning). No manual "add a price source to this
  product" or per-source quick-edit here -- admin-site only ever exposed
  those from the product-detail Pricing sub-panel, not the standalone
  tab, so they stay out of scope until Products gets a detail sub-view
  (same boundary Video Candidates' reassign-only-from-standalone-tab
  already established).
- **Cores** (`/cores`) -- the "other direction" view of
  `products.core_id`: one row per physical core (Al's example: DV8's
  Collision core, shared by six differently-named balls), with a
  product-count badge so a many-products-to-one-core case is visible at
  a glance. Search-by-name/brand-id filters, a "Products" button per row
  opening a `Modal` listing every product pointing at that core (a
  zero-product core is a real, useful signal -- a likely-orphaned row --
  shown as such rather than hidden). Read-only, matching admin_api --
  there's no create/update/delete endpoint for cores at all; rows come
  from the scrapers' own `get_or_create_core_id`, never by hand through
  this API.
- **Coverstocks** (`/coverstocks`) -- the exact same "other direction"
  view as Cores, one migration later (008): one row per named
  coverstock formulation, scoped to a brand, with a product_count.
  Read-only, matching admin_api -- no create/update/delete endpoints
  for coverstocks either. Structurally CoresPage.tsx with
  core_type/release_era swapped for material/type; kept as its own file
  rather than a shared generic component, same call CoresPage itself
  made.
- **Blocked Channels** (`/blocked-channels`) -- an admin-curated
  denylist (021_blocked_video_channels.sql) of YouTube channel display
  names whose videos never get pushed to BigCommerce's Product Videos
  feature via `src/bowlerdepot_video_sync`. On admin-site this panel
  lives inside the Video Candidates tab; here it's its own top-level
  page, matching the rest of the SPA's one-page-per-resource pattern.
  Create/list/delete only (no update -- a row here IS the block).
  `VideoCandidatesPage.tsx` also got a "Block channel" quick-action per
  row, mirroring admin-site's `blockChannelForVideo`, so an admin
  spotting a competitor's video doesn't have to leave the review queue.
- **Batch Jobs** (`/batch-jobs`) -- the last remaining admin-site tab,
  now fully ported. Three parts: a single-product rollup refresh
  (`POST /products/{id}/refresh-video-summary`), six list-then-loop
  bulk operations (a reusable `BatchRunner<T>` generic component in
  `BatchJobsPage.tsx` replaces admin-site's config-driven
  `runBatch`/`BATCH_CONFIGS`, listing against one `GET /products`
  boolean filter and then calling one POST endpoint per product,
  sequentially, with a Stop button that takes effect between items --
  `running` is tracked in a ref, not just state, since the loop reads
  it on every iteration), and the Manual Seed URLs panel
  (027_manual_seed_urls.sql -- the orphan-page catch for a real, live
  product a manufacturer's site stopped linking to internally, list/
  create/delete against `/manual-seed-urls`, brand-scoped via a new
  `GET /brands`-backed dropdown). New `listBrands`/`Brand` in
  api/client.ts+types.ts -- turns out `GET /brands` already existed in
  admin_api (backs admin-site's own brand-picker dropdowns) even though
  this README's "not here yet" list still called out a missing brand
  filter API; that line has been corrected.
- A small hand-rolled component library in `src/components/` (`Button`,
  `Badge`, `Card`, `StatCard`, `Modal`, `Toast`, `DataTable`,
  `Pagination`, `Layout`, `ErrorBoundary`) that later tabs (Articles,
  Price Sites, Cores/Coverstocks...) can build on without re-solving
  sort/select/bulk-action each time.
- A React error boundary (`ErrorBoundary.tsx`) wrapped around `<Outlet/>`
  in `Layout.tsx`, keyed by route pathname so navigating away resets it.
  A bug in one page now shows an inline error card instead of blanking
  sign-in/navigation for the whole app -- see "Verified so far" below
  for the incident that prompted this.
- **"Dense pro-tool" dark theme** (2026-09-05) -- Al picked this from a
  set of style mockups over chat, replacing the earlier light slate/blue
  palette. See `DEPLOY_RUNBOOK.md`'s admin-SPA style-decision writeup for
  the full token design; the short version is a from-scratch `ink`
  grayscale in `tailwind.config.js` (every former `slate-N` class was
  mechanically renamed to `ink-N`, since the number-to-usage meaning
  didn't change, only the literal color), a single indigo `primary`
  accent, and `color-scheme: dark` on `body` so native `<select>`/
  `<input>`/checkbox chrome follows along without per-page classes.
- **Nav icons + collapsible sidebar** -- each `Layout.tsx` nav item now
  has a small hand-rolled inline SVG icon (`src/components/icons.tsx`;
  no icon library dependency, see that file's own comment on why). A
  chevron button in the sidebar header collapses it to icon-only
  (`w-14`, labels hidden, `title` attribute for a hover tooltip
  instead); the collapsed/expanded state is remembered in
  `localStorage` across reloads.
- **Mobile-responsive pass** (2026-09-05) -- scoped to Al's stated
  priority ("quick checks on the go"): Dashboard, Review Queue, Video
  Candidates, and Blocked Channels get real phone treatment; denser
  pages (Products, Batch Jobs, Price Sites, Cores, Coverstocks,
  Articles) just need to not visually break. `Layout.tsx`'s sidebar
  becomes an off-canvas drawer below the `md` breakpoint (hamburger to
  open, backdrop/close-button/nav-pick to close) instead of a
  permanently-reserved column. `DataTable.tsx` gets a CSS-only
  responsive mode: below `md` every table becomes a stack of bordered
  cards with each cell's column header injected via
  `data-label`/`before:content-[attr(...)]`, so this benefits every
  page using `DataTable` with no per-page changes. `Modal.tsx` needed
  no changes (already `w-full`/`max-w-*` with viewport padding); two
  raw `<table>`s inside Cores'/Coverstocks' own detail modals (they
  don't use `DataTable`) got an `overflow-x-auto` wrapper instead.
  Fixed-width filter/form inputs on the four priority pages became
  `w-full sm:w-{n}` so they don't sit oddly narrow when stacked; on the
  denser pages the existing `flex-wrap` filter bars and individually
  narrow (≤288px) field widths already fit a phone viewport without
  intervention. See `DEPLOY_RUNBOOK.md`'s admin-SPA section for the
  full writeup, including the one real bug this pass found and fixed
  (ArticlesPage's action/product-shot image row lacked `flex-wrap` and
  would have forced its own Modal to scroll sideways on a narrow phone).
- **Card dial-in pass** (2026-09-05, ArticlesPage) -- a follow-up to the
  mobile-responsive pass above, on the one page with the busiest card
  content. `DataTable.tsx` gained an opt-in `stackOnMobile` column flag
  (label above content, full card width) for columns whose content is
  a button group or a multi-line block rather than a short value --
  ArticlesPage's Title/BigCommerce/actions columns all use it now. The
  Title cell's leading thumbnail switched from an inline-block/
  align-middle trick to a `flex items-start` row so it stays pinned to
  the top-left regardless of whether the title wraps to one or two
  lines. The actions column's five buttons (Approve/Reject, Regen
  text, Regen images, Preview) are now two visual tiers -- the review
  decision on top, maintenance actions below -- instead of one flat
  row. The image-candidate picker cards in the article preview modal
  got `flex-col` + `mt-auto` on their trailing badge/button so it sits
  flush at the bottom of every card regardless of the row's stretched
  height, instead of floating wherever the text above happened to end.
  Follow-up: the Title column's thumbnail went from `h-8 w-8` (32px) to
  `h-12 w-12` (48px) to, after Al looked at it live, `h-20 w-20` (5rem/
  80px) -- 32px read as a colored square more than a recognizable ball
  photo. The "no images"/blank placeholders track the same size and
  the "no images" one keeps its `bg-ink-50` slot so it still reads as
  an empty image box rather than floating text.

## Auth model

Two Cognito groups map to two roles (`resolve_role_from_groups` in
`src/admin_api_authorizer/app.py`): `Admins` -> `admin`, `Editors` ->
`editor`. **Phase 1 does not yet enforce role-based route restrictions**
on the backend -- both roles can currently do everything an
authenticated caller can do. The frontend shows the caller's role as a
badge (see `Layout.tsx`) but that's a display nicety, not a security
boundary; real RBAC enforcement is documented as follow-up work.

The 17 existing automation scripts (`scripts/*.py`) are unaffected --
they keep using the pre-existing shared-secret bearer token, which
`admin_api_authorizer` still accepts as a fallback whenever the
Cognito path doesn't apply (see that Lambda's module docstring).

## Local dev

```bash
cd admin-spa
npm install
cp .env.example .env.local   # fill in the four VITE_ values (see below)
npm run dev
```

`VITE_ADMIN_API_URL`, `VITE_COGNITO_USER_POOL_ID`, and
`VITE_COGNITO_CLIENT_ID` come off the main stack's CloudFormation
outputs:

```bash
aws cloudformation describe-stacks --stack-name <your-stack-name> \
  --query "Stacks[0].Outputs[?OutputKey=='AdminUserPoolId'].OutputValue" --output text
aws cloudformation describe-stacks --stack-name <your-stack-name> \
  --query "Stacks[0].Outputs[?OutputKey=='AdminUserPoolClientId'].OutputValue" --output text
```

`VITE_ADMIN_API_URL` is `AdminApiUrl` from those same stack outputs --
the existing `AdminHttpApi` invoke URL (same one
`admin-site/index.html`'s config bar points at).

### Creating your first admin account

There's no sign-up form -- create accounts via the AWS CLI (or the
Cognito console) and add them to a group:

```bash
aws cognito-idp admin-create-user --user-pool-id <AdminUserPoolId> \
  --username you@example.com --user-attributes Name=email,Value=you@example.com \
  --temporary-password '<TempPass123!>'
aws cognito-idp admin-add-user-to-group --user-pool-id <AdminUserPoolId> \
  --username you@example.com --group-name Admins
```

First sign-in with a temporary password will prompt Cognito to require
a new password -- phase 1's login form doesn't yet handle that
challenge (see "What's not here yet" below), so set a permanent
password up front instead:

```bash
aws cognito-idp admin-set-user-password --user-pool-id <AdminUserPoolId> \
  --username you@example.com --password '<YourRealPassword123!>' --permanent
```

## Build + deploy

```bash
npm run build          # outputs to dist/
```

See the main repo's `DEPLOY_RUNBOOK.md` (admin SPA section) for the S3
sync + CloudFront invalidation commands -- `AdminSiteBucket`/
`AdminSiteDistribution` in `template.yaml` are the hosting
infrastructure this build gets deployed to, and
`.github/workflows/deploy-admin-site.yml` automates it on push to
`admin-spa/**`.

**First deploy needs one extra step**: this sandbox has no npm registry
access (see "What's not here yet" below), so no `package-lock.json` was
ever generated here. Run `npm install` locally once, commit the
resulting `package-lock.json`, and the CI workflow's `npm ci` step will
then work -- exactly the same bootstrapping consumer-site needed (see
its own git history for precedent).

## What's not here yet

- Role-based route/action restrictions (both `admin` and `editor` can
  currently do everything).
- A "set new password" form for the Cognito `newPasswordRequired`
  challenge -- first-time accounts need a permanent password set via
  the CLI (see above) rather than through the app itself.
- Bulk select/reassign/delete on the product detail page's own Videos
  sub-tab (admin-site's product panel has this; `ProductDetailPage.tsx`
  currently does per-row approve/reject/restore/reassign only, same as
  the standalone Video Candidates tab -- see 2026-09-06's DEPLOY_
  RUNBOOK.md entry for the sub-tabs page this note used to be about).
- A real brand-name dropdown on the Products/Cores/Coverstocks filter
  bars (currently a raw brand-id text field on each) -- `GET /brands`
  does exist in admin_api (confirmed while building Batch Jobs' Manual
  Seed URLs panel, which does use it) and could back one; nobody has
  wired it into those three filter bars yet.
- Automated tests.

## Verified so far

Sign-in, Dashboard, and Products (including bulk rescrape) have been
smoke-tested against a real deployed stack -- Cognito login, KPI data,
and a queued rescrape all confirmed working end to end. `tsc -b`
compiles clean for the whole app (verified directly, not just written
against documented types). A real `npm run build`/`vite build` has
**not** been run successfully yet in this sandbox -- it fails here on a
platform mismatch (this sandbox is Linux, `node_modules` was installed
on a Mac, and `rollup`'s native binary is platform-specific; see
https://github.com/npm/cli/issues/4828), not a code issue, but it means
the production bundle itself is unverified. Run `npm run build`
yourself before the first real CloudFront deploy to confirm.

One real gotcha already hit and fixed: `amazon-cognito-identity-js`
pulls in Node's `buffer` package, which assumes a global `global`
object that doesn't exist in a browser. Vite doesn't polyfill Node
globals the way webpack did, so this threw `ReferenceError: global is
not defined` and blanked the whole app on first load. Fixed via
`define: { global: "globalThis" }` in `vite.config.ts` -- if a similar
`process is not defined`/`Buffer is not defined` error ever shows up
from the same dependency chain, that needs `vite-plugin-node-polyfills`
(or a manual shim) added there too.

Review Queue (approve/reject/bulk) has since been smoke-tested against
real data too -- confirmed working.

A second real gotcha, this time on Video Candidates' first browser
load: `match_confidence` was typed as `number | null` and rendered
with `.toFixed(2)`, but the actual column
(`db/migrations/004_product_videos.sql`) is a `'high'|'low'` text
enum -- threw `TypeError: r.match_confidence.toFixed is not a
function` and, because nothing in the tree caught the render error,
blanked the entire app rather than just that page. Fixed by retyping
the field and rendering it as a `Badge` instead, and by adding the
`ErrorBoundary` described above so a future mistake like this is
contained to one page instead of taking down sign-in and navigation
too. `tsc -b` passing clean does not catch this class of bug -- it's a
wrong assumption about a runtime value's shape, not a type error.

Price Sites has NOT been smoke-tested against real deployed data yet
either -- `tsc -b` passes clean but that's it. `match_confidence` on
`PriceSource` was typed as `"high" | "low" | null` by reading
`db/migrations/014_price_tracking.sql` directly first, specifically
because of the match_confidence incident above -- worth confirming that
holds up against a real row regardless.

Cores is the simplest tab so far (read-only, no review workflow) and
also NOT yet smoke-tested against real data -- `tsc -b` passes clean
only.

Coverstocks is structurally identical to Cores (same read-only shape,
one migration later). material/type are Postgres enum columns
(coverstock_material/coverstock_type) but admin_api returns them as
plain strings, confirmed by reading service.py's list_coverstocks
directly rather than assuming. **Update:** Al clicked through Cores,
Coverstocks, Blocked Channels, and Batch Jobs against the real deployed
stack on 2026-09-05 and confirmed all four "are just as good as the
originals" -- the per-tab caveats above/below are superseded for basic
functionality; anything more exotic than a first pass (Batch Jobs' Stop
button mid-loop, an exact-multiple-of-200-rows pagination edge case,
etc.) still hasn't specifically been exercised.

Blocked Channels' quick-block button on VideoCandidatesPage's actions
column is new surface area confirmed working in that same real
click-through: a blocked channel shows up on `/blocked-channels`
afterward, and blocking an already-blocked channel (case-insensitive
dedupe, per the migration's own unique index) doesn't surface a
confusing error to the user.

Articles has NOT been smoke-tested against real deployed data yet --
`tsc -b` passes clean, but given the match_confidence incident above,
treat that as necessary and not sufficient. Fields most worth watching
on first real load: `comparison_table` (typed as a loose
`Record<string, unknown>[]` -- deliberately not modeled field-by-field
since its shape has grown ad hoc, see `api/types.ts`'s own comment) and
`seed` on image candidates (should be `null` for every Gemini
candidate, a number for Stability ones).

Batch Jobs got a first real click-through from Al on 2026-09-05 (see the
"Update" note above) confirming the tab holds up in general use -- this
is still the most code-heavy tab ported so far (a generic `BatchRunner<T>`
component driving a client-side list-then-loop against real product IDs,
six times over with two different result shapes), so some scenarios are
still unexercised. Most worth confirming next: the Stop button actually
halts the loop between items
(the `runningRef` pattern was chosen deliberately over plain state
specifically to avoid a stale-closure bug here, but hasn't been
exercised against a real multi-page product list); the "Refresh ALL"
confirm dialog fires before any real Bedrock spend; and that
`listProducts`'s existing pagination (`page.length < limit` as the
stop condition) doesn't silently truncate a filter that returns exactly
a multiple of 200 rows. Manual Seed URLs' brand dropdown is the first
real use of the `GET /brands` endpoint in admin-spa -- confirm it
actually populates from a real deployed stack.

The dense-pro-tool restyle has NOT been visually verified in a running
browser -- `tsc -b` passes clean, and every color-token usage was
manually traced (see `DEPLOY_RUNBOOK.md`) to catch same-tone
hover-on-surface collisions (three found and fixed: nav-item hover,
ghost-button hover, table-row hover all previously resolved to the same
color as the surface they sit on). But `npm run dev`/`vite build` still
hit the same platform-mismatch failure noted above (this sandbox is
Linux, `node_modules` is Mac-built), so nothing here has actually been
rendered and looked at. **First thing to do on a real machine: run
`npm run dev` and eyeball every page**, especially the two hover states
called out above and anywhere text sits directly on a `bg-{role}-light`
chip (badges, error banners, the Toast) -- those were sized by contrast
math, not by looking at them.

The mobile-responsive pass (see "What's here" above) has the same
limitation, one level deeper: this sandbox can't run a real browser at
all, mobile or desktop, so nothing here has been checked at an actual
~375px width either -- only `tsc -b --force` (clean) and manual tracing
of each Tailwind breakpoint class against the CSS it should produce.
Two specific things worth a real phone (or a resized desktop browser)
before trusting this: the `content-[attr(data-label)]` labels in
`DataTable.tsx` depend on Tailwind's arbitrary-value content utility
actually compiling as expected (confirmed supported since 3.3, this
project pins `^3.4.10`, but "should compile" and "renders correctly at
this exact viewport" are different claims); and the off-canvas sidebar
drawer in `Layout.tsx` (`fixed` + `translate-x-full`/`translate-x-0` +
a `z-30` backdrop) has only been reasoned through, never seen animate.
