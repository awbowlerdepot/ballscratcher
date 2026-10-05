import { Link } from "react-router-dom";
import { articleHref, resizedImageUrl } from "../../api/client";
import type { LearnPlotterPoint } from "../../api/types";
import { ROLES, TENTHS_ZOOM, formatPosition, sourceLabel } from "./plotterModel";

// The selected ball and what to look at next (runbook 6cc): similar balls
// from other brands, then the nearest step in each direction. Every
// suggestion is something BowlerDepot sells (Al's choice), so each gets a
// Shop link, plus a Read-the-review link when there's a Learn article.
// Clicking a suggestion's name re-centers the plotter on it.

interface Props {
  selected: LearnPlotterPoint;
  byId: Map<string, LearnPlotterPoint>;
  onSelect: (id: string) => void;
  compact?: boolean;
}

function BallThumb({ p, size }: { p: LearnPlotterPoint; size: number }) {
  const src = p.primary_image_url
    ? resizedImageUrl(p.primary_image_url, { w: size * 2, h: size * 2, fit: "contain", fmt: "webp" })
    : null;
  return src ? (
    <img src={src} alt="" width={size} height={size} className="shrink-0 rounded-full bg-paper" loading="lazy" />
  ) : (
    <span className="shrink-0 rounded-full bg-paper" style={{ width: size, height: size }} />
  );
}

function EstimatedBadge({ p }: { p: LearnPlotterPoint }) {
  if (p.oil_motion_source !== "estimated") return null;
  return (
    <span
      className="rounded border border-dashed border-muted/60 px-1 text-[10px] uppercase tracking-wide text-muted"
      title="Estimated from the ball's cover, finish, core specs and price -- not from a manufacturer chart."
    >
      est.
    </span>
  );
}

function BallLinks({ p }: { p: LearnPlotterPoint }) {
  return (
    <span className="flex gap-3 text-xs font-semibold">
      {p.article_slug ? <Link to={articleHref({ slug: p.article_slug })}>Read review</Link> : null}
      {p.ecommerce_url ? (
        <a href={p.ecommerce_url} target="_blank" rel="noreferrer">
          Shop &rarr;
        </a>
      ) : null}
    </span>
  );
}

export default function PlotterNeighborPanel({ selected, byId, onSelect, compact = false }: Props) {
  return (
    <div className="flex flex-col gap-5">
      {!compact ? (
        <div className="flex gap-3">
          <BallThumb p={selected} size={64} />
          <div className="min-w-0">
            <p className="text-xs font-semibold uppercase tracking-wide text-muted">{selected.brand_name}</p>
            <p className="font-display text-lg font-semibold leading-tight text-ink">{selected.name}</p>
            <p className="mt-1 text-xs text-muted">
              {formatPosition(selected, TENTHS_ZOOM)} · {sourceLabel(selected.oil_motion_source)}
            </p>
            {selected.coverstock_name ? <p className="text-xs text-muted">{selected.coverstock_name}</p> : null}
            <div className="mt-1">
              <BallLinks p={selected} />
            </div>
          </div>
        </div>
      ) : null}

      {ROLES.map((role) => {
        const items = (selected.neighbors[role.key] ?? []).map((n) => byId.get(n.id)).filter(Boolean) as LearnPlotterPoint[];
        return (
          <section key={role.key}>
            <h3 className="flex items-center gap-2 font-display text-sm font-semibold text-ink">
              <span className="inline-block h-2.5 w-2.5 rounded-full" style={{ background: role.color }} />
              {role.label}
            </h3>
            {!compact ? <p className="mb-1.5 text-xs text-muted">{role.blurb}</p> : null}
            {items.length ? (
              <ul className="mt-1 flex list-none flex-col gap-2 p-0">
                {items.map((p) => (
                  <li key={p.id} className="flex items-center gap-2.5">
                    <BallThumb p={p} size={36} />
                    <div className="min-w-0">
                      <button
                        type="button"
                        onClick={() => onSelect(p.id)}
                        className="block max-w-full truncate text-left text-sm font-semibold text-ink hover:text-accent"
                        title={`Show ${p.brand_name} ${p.name} on the plotter`}
                      >
                        {p.brand_name} {p.name}
                      </button>
                      <span className="flex flex-wrap items-center gap-x-2 text-xs text-muted">
                        {formatPosition(p, TENTHS_ZOOM)} <EstimatedBadge p={p} />
                      </span>
                      <BallLinks p={p} />
                    </div>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="mt-1 text-xs text-muted">
                {role.key === "twins"
                  ? "No other brand's ball sits this close."
                  : "Nothing BowlerDepot carries a step in this direction."}
              </p>
            )}
          </section>
        );
      })}
    </div>
  );
}
