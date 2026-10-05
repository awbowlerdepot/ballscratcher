# BowlerIQ Partner API v1

Read-only bowling ball data for BowlerIQ partner platforms. It covers specs per weight, cover and core, ball motion plotter positions with suggestions, and links to Learn reviews and BowlerDepot.

- **Base URL:** `https://api.bowleriq.io/partner/`
- **Auth:** send `Authorization: Bearer <your partner key>` on every request.
  - Keys are issued per platform. Keep yours server-side; never ship it to a browser or mobile app.
  - A missing or wrong key gets `401`/`403`.
- **Format:** JSON. Every response includes `"api_version": "1"`.
- **Rate limit:** about 10 requests/second, with bursts of 20. Over that you get `429`; back off and retry.
- **Schema:** the full OpenAPI 3 schema is `GET /partner/openapi.json` (with your key), and is also provided as a file, `partner_api_v1_openapi.json`.

## The v1 promise

The shape of v1 only **grows**. We may add new optional fields and new endpoints. Your client should ignore fields it doesn't know.

We will never, within v1:

- rename or remove a field or endpoint
- change a field's type, units or meaning
- make an optional field required

Any of those ships as `/v2`, and `/v1` keeps running while you migrate. Internal changes to BowlerIQ's own sites don't affect this API.

## Endpoints

| Endpoint | What it returns |
|---|---|
| `GET /v1/brands` | Brands with at least one published ball |
| `GET /v1/balls?cursor=&limit=` | Every published ball (current and retired), paged by id. Good for a one-time full load. |
| `GET /v1/balls/{id}` | One ball; `404` if it doesn't exist or isn't published |
| `GET /v1/changes?cursor=&since=&limit=` | **The sync feed** (below) |
| `GET /v1/plotter` | Every current ball's plotter position, plus similar balls from other brands and the nearest step toward more oil, less oil, more angular and smoother |

`limit` defaults to 200, maximum 500.

### Ball

Values below are illustrative.

```json
{
  "id": "ff32beb3-57fc-4c61-abb4-c4cab2b46240",
  "name": "PHAZE II",
  "brand": {"id": "…", "name": "Storm"},
  "status": "current",
  "release_date": "2018-01-25",
  "color": "Pitch Purple",
  "coverstock": {"name": "TX-16 Solid", "material": "reactive_resin", "type": "solid"},
  "core": {"name": "Velocity", "type": "symmetric"},
  "has_particle": false,
  "factory_finish": "1500 Grit Polished",
  "finish_category": "polished",
  "weights": [{"weight_lbs": 16, "rg": 2.48, "differential": 0.051, "mass_bias": null}],
  "plotter": {"oil": 9.1, "motion": 14.4, "source": "estimated"},
  "image_url": "https://…",
  "manufacturer_url": "https://www.stormbowling.com/…",
  "learn_article_url": "https://learn.bowlerdepot.com/articles/storm-phaze-ii",
  "bowlerdepot_url": "https://bowlerdepot.com/…",
  "content_changed_at": "2026-10-05T15:42:10.123Z"
}
```

- **`id`:** stable, and safe to use as your key. Each colorway is its own ball.
- **`status`:** `"retired"` means the ball is no longer made. It's still valid data.
- **`weights`:** listed heaviest first. RG and differential are in inches. `mass_bias` is null for symmetric cores.
- **`plotter.oil`:** 1.0 (light) to 16.0 (heavy oil).
- **`plotter.motion`:** 1.0 (smooth) to 18.0 (angular).
- **`plotter.source`:**
  - `chart`: read off a manufacturer's published ball motion chart
  - `estimated`: BowlerIQ's model
  - `manual`: a staff correction
- **Nullable fields:** any field whose type includes null can be `null`, e.g. a ball with no specs published yet.

## Syncing (recommended)

```text
cursor = load_saved_cursor()                    # None on the very first run
loop:
    page = GET /v1/changes?cursor=<cursor>      # omit cursor the first time
    for change in page.items:
        if change.change == "upsert": save(change.ball)   # create or replace by change.id
        if change.change == "remove": delete(change.id)
    cursor = page.next_cursor
    save_cursor(cursor)                         # after applying the page
    if not page.has_more: break
```

Run it on a schedule. Every 15–60 minutes is plenty.

- The **first run** (no cursor) returns every published ball as an `upsert`. To start from a point in time instead, pass `?since=2026-10-01T00:00:00Z`.
- **`next_cursor` is always present.** Store it after applying each page. If a run crashes, re-running from the last saved cursor is safe, because upserts and removes can be applied more than once.
- **`remove`** means the ball is no longer published. Delete your copy.
- A ball appears once per page with its **latest** state, not once per edit.
- Changes show up about **60 seconds** after they happen. The newest minute is held back so nothing is skipped mid-write.
- A ball counts as **changed** when any of these change:
  - its own fields
  - any weight's specs
  - its images
  - its Learn review link

  Routine re-scrapes that change nothing don't count.

## Errors

| Status | Meaning |
|---|---|
| `400` | Bad `cursor` or `since`. Restart from your last good cursor. |
| `401` / `403` | Missing or invalid key |
| `404` | Ball not found or not published |
| `429` | Rate limited; retry with backoff |
| `5xx` | Our problem; retry with backoff |

## Example

```bash
curl -s -H "Authorization: Bearer $BOWLERIQ_PARTNER_KEY" \
  "https://api.bowleriq.io/partner/v1/changes?limit=50"
```
