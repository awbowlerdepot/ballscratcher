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
- Every other admin-site tab: Blocked Channels, Batch Jobs. These still
  live on `admin-site/index.html` for now; migrating them is follow-up
  work.
- A Products detail sub-view (the old admin-site has a tabbed per-
  product panel with its own Videos section, "search again" rescan
  button, and bulk reassign/delete -- admin-spa's Video Candidates tab
  only covers the standalone list, not that richer per-product view).
- A real brand-name dropdown on the Products filter bar (currently a
  raw brand-id text field -- there's no `GET /brands` on the admin API
  the way `consumer-site` has on the public one).
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
one migration later) and also NOT yet smoke-tested against real data --
`tsc -b` passes clean only. material/type are Postgres enum columns
(coverstock_material/coverstock_type) but admin_api returns them as
plain strings, confirmed by reading service.py's list_coverstocks
directly rather than assuming.

Articles has NOT been smoke-tested against real deployed data yet --
`tsc -b` passes clean, but given the match_confidence incident above,
treat that as necessary and not sufficient. Fields most worth watching
on first real load: `comparison_table` (typed as a loose
`Record<string, unknown>[]` -- deliberately not modeled field-by-field
since its shape has grown ad hoc, see `api/types.ts`'s own comment) and
`seed` on image candidates (should be `null` for every Gemini
candidate, a number for Stability ones).

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
