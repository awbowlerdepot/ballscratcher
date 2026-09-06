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
- A small hand-rolled component library in `src/components/` (`Button`,
  `Badge`, `Card`, `StatCard`, `Modal`, `Toast`, `DataTable`,
  `Pagination`, `Layout`) that later tabs (Video Candidates, Articles,
  Price Sites, Cores/Coverstocks...) can build on without re-solving
  sort/select/bulk-action each time.

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
- Every other admin-site tab: Video Candidates, Articles, Price Sites,
  Cores, Coverstocks, Blocked Channels, Batch Jobs. These still live on
  `admin-site/index.html` for now; migrating them is follow-up work.
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

Review Queue (approve/reject/bulk) has not yet been smoke-tested
against real data -- it's `tsc`-clean but untried in the browser.
