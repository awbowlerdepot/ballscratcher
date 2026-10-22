# BowlerIQ Partner API v1: oil patterns (final spec)

This is the build spec for the oil pattern catalog, written by the BowlerIQ side
(runbook 6ej). It starts from Drilld's proposal (`bowleriq-oil-patterns-spec.md`)
and keeps its shape. The changes and clarifications are listed first, so Drilld can
see what moved. A copy sits next to the proposal for the Drilld sessions.

## Changes from the proposal

| # | What | Why |
|---|---|---|
| 1 | **`profile` cells are numbers to 0.1 µL**, not integers. Row and board totals still sum to the pattern volume within ±1 µL per row. | A step spreads its oil evenly over a distance that rarely lands on whole feet, so cells are fractional. Rounding each cell to an integer would make the grid stop summing to the sheet's volume. |
| 2 | **Profile rows are defined exactly:** `ceil(length_feet / foot_step)` rows. Row `i` covers feet `[i·foot_step, (i+1)·foot_step)`. Oil from a step whose distance runs past the last row's end is added to the last row. | The proposal said "0 to length_feet", which could be read as one extra row. |
| 3 | **How a step spreads its oil:** each board from `from_board` to `to_board` gets `loads × oil_per_board_ul`, spread evenly over the step's distance, `min(start_feet, end_feet)` to `max(...)`. A zero-length step puts it all in the row containing that foot. Forward and reverse are treated the same; direction only orders the program. | So every partner reads a sheet the same way. (Buffer carry-down and breakdown stay out of the profile, as the proposal says.) |
| 4 | **Steps also carry `feet`**: the sheet's distance column (signed; negative for reverse). | It's on every Kegel sheet and makes OCR checking easy. Optional. |
| 5 | **`track_zones[].label`** is the sheet's description, e.g. `Outside:Middle`. `from`/`to`/`against_from`/`against_to` stay as the sheet's labels. | As proposed. Converted board numbers aren't added, because zones are only read by people. |
| 6 | **`ratio`** is the largest `track_zones` ratio, as proposed. It's `null` when the sheet has no zones. | |
| 7 | **`category` adds `house`**, as proposed. Al: house shots are just another pattern for now; there are no center-private patterns yet. | |
| 8 | **`source` adds `upload`**: a sheet uploaded by BowlerIQ staff, with no public source URL. | That's how every pattern arrives at first (manual uploads). |
| 9 | **`sheet_url` and `image_url` point to the original publisher only.** They are `null` for uploads. BowlerIQ doesn't redistribute the sheet files. | Licensing. The data is the product; the PDF isn't. |
| 10 | **Only approved patterns are in the API.** Every pattern is reviewed by BowlerIQ staff before it's published (see Ingest). | Al: "they need approved before they are added to the live catalog. similar pattern to the videos." |

Everything else is as proposed: the three endpoints, the separate changes feed with
the same rules as `/v1/changes`, boards 1–39 from the left (`nL` → `n`,
`nR` → `40 − n`), units, nullable means unknown, the v1 promise and the errors.

## Endpoints

| Endpoint | Returns |
|---|---|
| `GET /v1/patterns?cursor=&limit=` | Every published pattern, paged by id. |
| `GET /v1/patterns/{id}` | One published pattern; `404` otherwise. |
| `GET /v1/patterns/changes?cursor=&since=&limit=` | The pattern sync feed. Items are `{"id", "change": "upsert", "changed_at", "pattern"}` or `{"id", "change": "remove", "changed_at", "pattern": null}`. `next_cursor` is always present and `has_more` tells you to keep paging. There's a ~60 s holdback. A first sync without a cursor skips removes. |

## Pattern object

As in the proposal's example, plus `feet` on steps and `profile_source`
(`"steps"` or `"graph"`). Fields:

- **Identity:** `id` (stable UUID), `name`, `short_name`, `category` (`house`,
  `sport`, `challenge`, `recreational`, `tournament`), `series`, `source`
  (`kegel`, `pba`, `usbc`, `wtba`, `brunswick`, `center`, `manual`, `upload`),
  `source_url`, `sheet_url`, `image_url` and `status` (`current`, `retired`).
- **Sheet header:**
  - `length_feet`, `reverse_brush_drop_feet`, `oil_per_board_ul`;
  - `forward_ml`, `reverse_ml`, `volume_ml`;
  - `ratio`, `track_zones[]`.
- **`machine`** (metadata): `make`, `model`, `tank_configuration`,
  `tank_a_conditioner`, `tank_b_conditioner`, `cleaner_ratio_main`,
  `cleaner_ratio_back_end`, `cleaner_back_end_distance_feet` and `buffer_rpm`
  (`{"1": rpm, …}`).
- **`steps[]`**, in program order:
  - `direction`, `order`, `start`, `stop`, `from_board`, `to_board`, `loads`;
  - `speed_ips`, `buffer`, `tank`, `crossed`;
  - `start_feet`, `end_feet`, `feet`, `oil_ul`.
- **`board_totals`:** `forward_ul[39]` and `reverse_ul[39]`. These are integers,
  computed from the steps.
- **`profile`:** `foot_step` (1), `units` (`"ul_per_board_per_foot"`),
  `rows[][39]`.
- **`content_changed_at`:** when anything above last changed.

## Ingest and review (BowlerIQ side)

1. **Upload.** Staff upload a pattern sheet (PDF) in admin. It's stored in a
   **private** bucket.
2. **Read.** A Lambda renders the page and reads it with Claude vision on
   Bedrock: the header, both step tables and the track zones. The result is
   saved as a **pending** pattern.
3. **Check.** On every view and save, admin recomputes everything from the
   steps and checks it against the sheet:
   - each step's `crossed = boards × loads`;
   - `oil_ul = crossed × oil_per_board_ul`;
   - forward and reverse sums against the header totals;
   - the volume (forward + reverse);
   - board ranges valid (1–39), distances within the pattern length, steps
     contiguous.

   A check that fails is shown on the review screen next to the step it
   concerns.
4. **Review.** Staff correct anything the OCR got wrong, then **Approve** (it's
   published and enters the feed) or **Reject**. Approving is blocked while any
   check fails.
5. **Later edits** to an approved pattern republish it: `content_changed_at`
   moves and the feed sends an upsert. Unpublishing sends a remove.

## Reference

The Elite 48 worked example, its tables and checks are in the proposal. BowlerIQ's
tests use them: the forward total is 14,650 µL and the reverse 15,550 µL; board 20
gets 1,100 µL (450 forward and 650 reverse) and boards 2–7 get 550 µL; the profile
sums to 30,200 µL.
