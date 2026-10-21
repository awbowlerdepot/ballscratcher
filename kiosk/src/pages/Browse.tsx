import { useEffect, useMemo, useState, type ReactNode } from "react";
import { getCurrentCards, getPlotter, type LearnPlotterPoint, type ProductCard } from "../lib/api";
import { useKiosk } from "../lib/kiosk";
import { BallCard, Chip, Grid, PageTitle, Spinner } from "../components/ui";

// Browse (runbook 6eh): the whole current catalog with touch filters.
// Brand, cover type and lane condition (the plotter's oil axis), sorted
// newest / most popular / A-Z. Filters reset with the session.

const OIL_BANDS: { key: string; label: string; range: [number, number] }[] = [
  { key: "dry", label: "Dry lanes", range: [1, 6.5] },
  { key: "medium", label: "Medium oil", range: [6.5, 11] },
  { key: "heavy", label: "Heavy oil", range: [11, 16.1] },
];
const COVERS = ["solid", "pearl", "hybrid"];
type Sort = "newest" | "popular" | "az";

export default function Browse() {
  const { sessionKey } = useKiosk();
  const [cards, setCards] = useState<ProductCard[] | null>(null);
  const [plotter, setPlotter] = useState<Map<string, LearnPlotterPoint>>(new Map());
  const [brand, setBrand] = useState<string | null>(null);
  const [cover, setCover] = useState<string | null>(null);
  const [oil, setOil] = useState<string | null>(null);
  const [sort, setSort] = useState<Sort>("newest");

  useEffect(() => {
    setBrand(null);
    setCover(null);
    setOil(null);
    setSort("newest");
  }, [sessionKey]);

  useEffect(() => {
    getCurrentCards()
      .then(setCards)
      .catch(() => setCards([]));
    getPlotter()
      .then((ps) => setPlotter(new Map(ps.map((p) => [p.id, p]))))
      .catch(() => undefined);
  }, []);

  const brands = useMemo(() => [...new Set((cards ?? []).map((c) => c.brand_name))].sort(), [cards]);
  const shown = useMemo(() => {
    const band = OIL_BANDS.find((b) => b.key === oil)?.range;
    const list = (cards ?? []).filter(
      (c) =>
        (!brand || c.brand_name === brand) &&
        (!cover || c.coverstock_type === cover) &&
        (!band || ((p) => p && p.oil >= band[0] && p.oil < band[1])(plotter.get(c.id))),
    );
    const sorted = [...list];
    if (sort === "az") sorted.sort((a, b) => `${a.brand_name} ${a.name}`.localeCompare(`${b.brand_name} ${b.name}`));
    else if (sort === "popular") sorted.sort((a, b) => (b.popularity_score ?? 0) - (a.popularity_score ?? 0));
    else sorted.sort((a, b) => (b.release_date ?? "").localeCompare(a.release_date ?? ""));
    return sorted;
  }, [cards, brand, cover, oil, sort, plotter]);

  return (
    <div>
      <PageTitle kicker="Browse" title="Every current ball" sub={cards ? `${shown.length} of ${cards.length} balls` : undefined} />
      <div className="mb-8 flex flex-col gap-4 rounded-3xl border-2 border-edge bg-panel p-6">
        <FilterRow label="Brand">
          <Chip active={!brand} onClick={() => setBrand(null)}>
            All
          </Chip>
          {brands.map((b) => (
            <Chip key={b} active={brand === b} onClick={() => setBrand(brand === b ? null : b)}>
              {b}
            </Chip>
          ))}
        </FilterRow>
        <FilterRow label="Cover">
          <Chip active={!cover} onClick={() => setCover(null)}>
            Any
          </Chip>
          {COVERS.map((c) => (
            <Chip key={c} active={cover === c} onClick={() => setCover(cover === c ? null : c)}>
              {c[0].toUpperCase() + c.slice(1)}
            </Chip>
          ))}
        </FilterRow>
        <FilterRow label="Lanes">
          <Chip active={!oil} onClick={() => setOil(null)}>
            Any
          </Chip>
          {OIL_BANDS.map((b) => (
            <Chip key={b.key} active={oil === b.key} onClick={() => setOil(oil === b.key ? null : b.key)}>
              {b.label}
            </Chip>
          ))}
          <span className="mx-4 h-8 w-0.5 bg-edge" />
          <span className="text-lg font-semibold text-mute">Sort</span>
          {(
            [
              ["newest", "Newest"],
              ["popular", "Most popular"],
              ["az", "A–Z"],
            ] as [Sort, string][]
          ).map(([k, l]) => (
            <Chip key={k} active={sort === k} onClick={() => setSort(k)}>
              {l}
            </Chip>
          ))}
        </FilterRow>
      </div>
      {!cards ? (
        <Spinner />
      ) : shown.length ? (
        <Grid>
          {shown.map((c) => (
            <BallCard key={c.id} ball={c} caption={[c.coverstock_type, c.core_type].filter(Boolean).join(" · ")} />
          ))}
        </Grid>
      ) : (
        <p className="text-2xl text-mute">No balls match those filters. Try removing one.</p>
      )}
    </div>
  );
}

function FilterRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center gap-3">
      <span className="w-24 text-lg font-semibold text-mute">{label}</span>
      {children}
    </div>
  );
}
