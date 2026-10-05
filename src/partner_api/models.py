"""
Partner API v1 response models -- THE CONTRACT (DEPLOY_RUNBOOK.md 6cj).

Al: "One other thing that might be helpful is locking the shape down so
that if we move the learn and consumer sites forward it doesn't break the
integration to other platform." So these models are deliberately separate
from public_api's response dicts: the Learn/consumer sites can reshape
public_api freely, and nothing here moves unless someone edits THIS file.

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
    name: str


class Coverstock(_Model):
    name: Optional[str] = Field(None, description="Manufacturer's coverstock name, e.g. 'HK22 - Aggression Pearl'.")
    material: Optional[CoverstockMaterial] = None
    type: Optional[CoverstockType] = None


class Core(_Model):
    name: Optional[str] = None
    type: Optional[CoreType] = None


class Weight(_Model):
    weight_lbs: int
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
    release_date: Optional[date] = None
    color: Optional[str] = None
    coverstock: Coverstock
    core: Core
    has_particle: bool
    factory_finish: Optional[str] = Field(None, description="Manufacturer's finish text, e.g. '500, 1000, 1500 Siaair / Factory Compound'.")
    finish_category: Optional[FinishCategory] = None
    weights: list[Weight] = Field(description="Specs per weight, heaviest first. May be empty.")
    plotter: Optional[PlotterPosition] = None
    image_url: Optional[str] = None
    manufacturer_url: str
    learn_article_url: Optional[str] = Field(None, description="BowlerDepot Learn review, when one is published.")
    bowlerdepot_url: Optional[str] = Field(None, description="BowlerDepot product page, when BowlerDepot carries the ball.")
    content_changed_at: datetime = Field(description="When anything in this object last changed.")


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
    changed_at: datetime
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
    more_oil: list[PlotterNeighbor]
    less_oil: list[PlotterNeighbor]
    more_angular: list[PlotterNeighbor]
    smoother: list[PlotterNeighbor]


class PlotterEntry(_Model):
    id: str = Field(description="Ball id.")
    plotter: PlotterPosition
    neighbors: PlotterNeighbors = Field(
        description="Suggestions are limited to balls BowlerDepot sells; colorways of the same ball are collapsed."
    )


class PlotterList(_Model):
    api_version: Literal["1"] = API_VERSION
    items: list[PlotterEntry] = Field(description="Every current, published ball.")
