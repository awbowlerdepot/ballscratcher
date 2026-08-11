# reference/

Design references, not part of the deployed system -- nothing here is
imported by any Lambda, script, or test.

## plotter_reference.html / plotter_reference_balls.js

The interactive "Ball Motion Comparison" plotter Al built in another
Cowork project and shared with this one (originally at
`/Users/awolfe3/Downloads/site/index.html` + `balls.js`), kept here as
the visual/interaction reference for the eventual React plotter page
(see DEPLOY_RUNBOOK.md 6m and this session's task list: "Scaffold React
SPA" / "Integrate existing bowling ball plotter"). A vanilla JS/CSS
single-page scatter chart -- brand-filter chips, search, size slider,
label toggle, hover tooltips, click-through to a per-ball page.

Its actual DATA (56 balls, hand-digitized from Brunswick's own published
Ball Motion Comparison Chart PDF) has already been extracted into
`scripts/data/brunswick_chart_positions.json` and is being matched onto
real catalog products via `scripts/backfill_plotter_chart_positions.py`
-- these two files are kept only for their UI/CSS/interaction design,
not as a live data source. Its per-ball placeholder pages and images
(`balls/*.html`, `images/*.png` in the original folder) were NOT copied
here -- they were explicitly marked "Starter page... Replace with your
own content" and are superseded by this project's own real product
detail pages (`public_api`'s `GET /products/{id}`).
