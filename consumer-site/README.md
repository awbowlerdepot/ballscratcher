# consumer-site

The consumer-facing bowling ball site -- Vite + React + TypeScript SPA,
client-side routed (react-router), talking to `PublicApiFunction` (the
unauthenticated read-only API in the main repo's `src/public_api/`).

Four pages, one shell (`App.tsx` + `Nav.tsx`), no full page reloads
between them:

- **Browse** (`/`) -- current/retired toggle, brand filter, search,
  "add to compare" per card.
- **Ball detail** (`/balls/:id`) -- high-level specs, the video-summary
  rollup ("what reviewers are saying"), embedded YouTube reviews, and
  (for a retired ball) suggested current balls that compare closest.
- **Compare** (`/compare`) -- add up to 6 balls (matches
  `public_api.MAX_COMPARE_IDS`), see specs side by side. Selections
  persist in `localStorage` (shared with Browse/Detail's "add to
  compare" buttons) and mirror into `?ids=` so a compare set is a real
  shareable link.
- **Motion plotter** (`/plotter`) -- functional first pass over `GET
  /products/plotter`; see its own file comment for what's still a
  follow-up (porting the visual design from `reference/
  plotter_reference.html` in the main repo).

## Local dev

```bash
cd consumer-site
npm install
cp .env.example .env.local   # fill in VITE_PUBLIC_API_URL (see below)
npm run dev
```

`VITE_PUBLIC_API_URL` should be `PublicApiUrl` from the main stack's
CloudFormation outputs:

```bash
aws cloudformation describe-stacks --stack-name <your-stack-name> \
  --query "Stacks[0].Outputs[?OutputKey=='PublicApiUrl'].OutputValue" --output text
```

## Build + deploy

```bash
npm run build          # outputs to dist/
```

See the main repo's `DEPLOY_RUNBOOK.md` (consumer site section) for the
S3 sync + CloudFront invalidation commands -- `ConsumerSiteBucket`/
`ConsumerSiteDistribution` in `template.yaml` are the hosting
infrastructure this build gets deployed to.

## What's not here yet

- The motion plotter's real visual design (brand-chip filters, size
  slider, styled tooltips) -- current page is data-correct but plain.
- Any SEO/meta-tag work, analytics, or a custom domain on CloudFront.
- Automated tests -- this sandbox has no network access to the npm
  registry, so `npm install`/a real build were never run here; review
  the TypeScript for typos with a bit more scrutiny than code that was
  actually compiled and tested.
