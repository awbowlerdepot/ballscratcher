"""
Partner API v1 response models -- THE CONTRACT (DEPLOY_RUNBOOK.md 6cj).

Al: "One other thing that might be helpful is locking the shape down so
that if we move the learn and consumer sites forward it doesn't break the
integration to other platform." So these models are deliberately separate
from public_api's response dicts: the Learn/consumer sites can reshape
public_api freely, and nothing here moves unless someone edits THIS file.

Docs (runbook 6ee): descriptions and examples here feed the public API
reference at https://api.bowleriq.io/partner/docs. Adding or rewording a
description or example changes the OpenAPI snapshot but not the contract;
regenerate the snapshot on purpose (see the contract test's docstring).

Rules for v1 (enforced by tests/test_partner_api_contract.py, which diffs
the generated OpenAPI schema against tests/fixtures/partner_api_v1_openapi.json):
  * Additive only: new OPTIONAL fields or new endpoints are fine (update
    the fixture in the same change, on purpose).
  * Never rename, remove, retype, or change the meaning/units of a field,
    and never make an optional field required. That is a /v2.
  * Every model forbids extra keys, so a stray dict key from a query can't
    silently leak into the response and become a de facto contract.
"""
from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

API_VERSION = "1"

CoverstockMaterial = Literal["reactive_resin", "urethane", "polyester_plastic"]
CoverstockType = Literal["solid", "pearl", "hybrid"]
CoreType = Literal["symmetric", "asymmetric"]
FinishCategory = Literal["polished", "satin", "dull"]
BallStatus = Literal["current", "retired"]
PositionSource = Literal["chart", "estimated", "manual"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Brand(_Model):
    id: str = Field(description="Stable brand id (UUID).")
    name: str = Field(description="Brand name as the manufacturer writes it, e.g. 'Storm', 'Roto Grip'.")


class Coverstock(_Model):
    name: Optional[str] = Field(None, description="Manufacturer's coverstock name, e.g. 'HK22 - Aggression Pearl'.")
    material: Optional[CoverstockMaterial] = Field(None, description="Cover chemistry family.")
    type: Optional[CoverstockType] = Field(None, description="Solid, pearl or hybrid cover.")


class Core(_Model):
    name: Optional[str] = Field(None, description="Manufacturer's core name, e.g. 'Velocity'.")
    type: Optional[CoreType] = Field(None, description="Symmetric or asymmetric core.")


class Weight(_Model):
    weight_lbs: int = Field(description="Ball weight in pounds (typically 10-16).")
    rg: Optional[float] = Field(None, description="Radius of gyration, inches (e.g. 2.49).")
    differential: Optional[float] = Field(None, description="Total differential, inches (e.g. 0.054).")
    mass_bias: Optional[float] = Field(None, description="Intermediate differential, inches. Null for symmetric cores.")


class PlotterPosition(_Model):
    oil: float = Field(description="Oil the ball handles best: 1.0 (light) to 16.0 (heavy), to 0.1.")
    motion: float = Field(description="Backend motion shape: 1.0 (smooth) to 18.0 (angular), to 0.1.")
    source: PositionSource = Field(
        description="'chart' = read off a manufacturer's published ball motion chart; "
        "'estimated' = BowlerIQ's model; 'manual' = staff correction."
    )


class Ball(_Model):
    id: str = Field(description="Stable ball id (UUID). Colorways are separate balls.")
    name: str
    brand: Brand
    status: BallStatus = Field(description="'retired' = no longer made; still a valid ball.")
    release_date: Optional[date] = Field(None, description="Manufacturer release date, when known.")
    color: Optional[str] = Field(None, description="Colorway as the manufacturer names it.")
    coverstock: Coverstock
    core: Core
    has_particle: bool = Field(description="True when the cover contains particle (additive) material.")
    factory_finish: Optional[str] = Field(None, description="Manufacturer's finish text, e.g. '500, 1000, 1500 Siaair / Factory Compound'.")
    finish_category: Optional[FinishCategory] = Field(None, description="The factory finish, grouped as polished, satin or dull.")
    weights: list[Weight] = Field(description="Specs per weight, heaviest first. May be empty.")
    plotter: Optional[PlotterPosition] = Field(None, description="Position on the ball motion plotter; null if the ball isn't plotted.")
    image_url: Optional[str] = Field(None, description="Product photo (resized, CDN-hosted).")
    manufacturer_url: str = Field(description="The ball's page on the manufacturer's site.")
    learn_article_url: Optional[str] = Field(None, description="BowlerDepot Learn review, when one is published.")
    bowlerdepot_url: Optional[str] = Field(None, description="BowlerDepot product page, when BowlerDepot carries the ball.")
    content_changed_at: datetime = Field(description="When anything in this object last changed.")

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [{
                "id": "ff32beb3-57fc-4c61-abb4-c4cab2b46240",
                "name": "PHAZE II",
                "brand": {"id": "6a1f0c1e-2b5d-4c55-9a51-0f4e1b2c3d4e", "name": "Storm"},
                "status": "current",
                "release_date": "2018-01-25",
                "color": "Pitch Purple",
                "coverstock": {"name": "TX-16 Solid", "material": "reactive_resin", "type": "solid"},
                "core": {"name": "Velocity", "type": "symmetric"},
                "has_particle": False,
                "factory_finish": "1500 Grit Polished",
                "finish_category": "polished",
                "weights": [
                    {"weight_lbs": 16, "rg": 2.48, "differential": 0.051, "mass_bias": None},
                    {"weight_lbs": 15, "rg": 2.49, "differential": 0.050, "mass_bias": None},
                ],
                "plotter": {"oil": 9.1, "motion": 14.4, "source": "estimated"},
                "image_url": "https://img.bowleriq.io/product-images/ff32beb3/detail.png?w=800",
                "manufacturer_url": "https://www.stormbowling.com/phaze-ii",
                "learn_article_url": "https://learn.bowlerdepot.com/articles/storm-phaze-ii/",
                "bowlerdepot_url": "https://bowlerdepot.com/storm-phaze-ii/",
                "content_changed_at": "2026-10-05T15:42:10.123Z",
            }]
        },
    )


class BrandList(_Model):
    api_version: Literal["1"] = API_VERSION
    items: list[Brand]


class BallPage(_Model):
    api_version: Literal["1"] = API_VERSION
    items: list[Ball]
    next_cursor: Optional[str] = Field(None, description="Pass as ?cursor= for the next page; null on the last page.")


class BallResponse(_Model):
    api_version: Literal["1"] = API_VERSION
    item: Ball


class Change(_Model):
    id: str = Field(description="Ball id.")
    change: Literal["upsert", "remove"] = Field(
        description="'upsert' = create or replace your copy with `ball`; 'remove' = delete your copy (the ball is no longer published)."
    )
    changed_at: datetime = Field(description="When the change happened.")
    ball: Optional[Ball] = Field(None, description="Present for 'upsert', null for 'remove'.")


class ChangePage(_Model):
    api_version: Literal["1"] = API_VERSION
    items: list[Change]
    next_cursor: str = Field(
        description="ALWAYS present. Store it and pass it as ?cursor= on your next sync to get only newer changes."
    )
    has_more: bool = Field(description="True if more changes are ready now; keep paging until false.")


class PlotterNeighbor(_Model):
    id: str = Field(description="Ball id.")
    distance: float = Field(description="Distance on the plotter, in plotter units.")


class PlotterNeighbors(_Model):
    twins: list[PlotterNeighbor] = Field(description="Closest balls from OTHER brands (within 1.5 units).")
    more_oil: list[PlotterNeighbor] = Field(description="Nearest step toward heavier oil.")
    less_oil: list[PlotterNeighbor] = Field(description="Nearest step toward lighter oil.")
    more_angular: list[PlotterNeighbor] = Field(description="Nearest step toward a more angular backend.")
    smoother: list[PlotterNeighbor] = Field(description="Nearest step toward a smoother backend.")


class PlotterEntry(_Model):
    id: str = Field(description="Ball id.")
    plotter: PlotterPosition
    neighbors: PlotterNeighbors = Field(
        description="Suggestions are limited to balls BowlerDepot sells; colorways of the same ball are collapsed."
    )


class PlotterList(_Model):
    api_version: Literal["1"] = API_VERSION
    items: list[PlotterEntry] = Field(description="Every current, published ball.")


# Error bodies, for the docs only (runbook 6ee). The API's own errors
# (400/404) are FastAPI's {"detail": ...}; 401/403/429 come from API
# Gateway (the key check and throttling) as {"message": ...}.
class ErrorDetail(_Model):
    detail: str = Field(description="What went wrong.")


class GatewayError(_Model):
    message: str = Field(description="'Unauthorized', 'Forbidden' or 'Too Many Requests'.")
