import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { MATERIAL_LABEL, getProduct, img, titleCase, type ProductDetail } from "../lib/api";
import { useKiosk } from "../lib/kiosk";
import { BigButton, PageTitle, Spinner } from "../components/ui";
import { usePlotterIndex } from "../components/plotterBits";

// Compare (runbook 6eh): 2-3 balls side by side -- the specs that drive
// ball choice, row by row, plus a small chart of just these balls on the
// plotter's oil x motion grid so the difference is visible at a glance.
const COLORS = ["#3d6bff", "#ffc857", "#ff5a5f"];

export default function Compare() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const { compare, toggleCompare, clearCompare } = useKiosk();
  const ids = useMemo(() => (params.get("ids") || compare.join(",")).split(",").filter(Boolean).slice(0, 3), [params, compare]);
  const [products, setProducts] = useState<(ProductDetail | null)[] | null>(null);
  const { byId } = usePlotterIndex();

  useEffect(() => {
    setProducts(null);
    Promise.all(ids.map((id) => getProduct(id).catch(() => null))).then(setProducts);
  }, [ids]);

  if (!ids.length) {
    return (
      <div>
        <PageTitle kicker="Compare" title="Pick balls to compare" sub="Tap “+ Compare” on up to three balls, then come back here." />
        <BigButton to="/browse">Browse balls</BigButton>
      </div>
    );
  }
  if (!products) return <Spinner />;
  const list = products.filter(Boolean) as ProductDetail[];
  const heaviest = (p: ProductDetail) => [...(p.skus ?? [])].sort((a, b) => b.weight_lbs - a.weight_lbs)[0];

  const rows: [string, (p: ProductDetail) => string][] = [
    ["Cover", (p) => p.coverstock_full_name || p.coverstock_name || "--"],
    ["Cover type", (p) => titleCase(p.coverstock_type) || "--"],
    ["Material", (p) => (p.coverstock_material ? MATERIAL_LABEL[p.coverstock_material] ?? p.coverstock_material : "--")],
    ["Finish", (p) => p.factory_finish || "--"],
    ["Core", (p) => [p.core_name, p.core_type && titleCase(p.core_type)].filter(Boolean).join(" · ") || "--"],
    ["RG (heaviest)", (p) => (heaviest(p)?.rg != null ? `${heaviest(p)!.rg!.toFixed(2)} @ ${heaviest(p)!.weight_lbs} lb` : "--")],
    ["Differential", (p) => (heaviest(p)?.differential != null ? heaviest(p)!.differential!.toFixed(3) : "--")],
    ["Mass bias", (p) => (heaviest(p)?.mass_bias != null ? heaviest(p)!.mass_bias!.toFixed(3) : "--")],
    ["Lane oil", (p) => (byId.get(p.id) ? `${byId.get(p.id)!.oil.toFixed(1)} of 16` : "--")],
    ["Motion", (p) => (byId.get(p.id) ? `${byId.get(p.id)!.motion.toFixed(1)} of 18` : "--")],
    ["Review videos", (p) => String(p.video_reviews_summary_video_count ?? 0)],
  ];

  return (
    <div>
      <div className="flex items-start justify-between gap-6">
        <PageTitle kicker="Compare" title={list.map((p) => p.name).join(" vs ")} />
        <BigButton
          tone="ghost"
          onClick={() => {
            clearCompare();
            navigate("/browse");
          }}
        >
          Clear compare
        </BigButton>
      </div>
      <div className="grid grid-cols-[minmax(0,3fr)_minmax(0,2fr)] gap-8">
        <div className="overflow-hidden rounded-3xl border-2 border-edge bg-panel">
          <div className="grid" style={{ gridTemplateColumns: `200px repeat(${list.length}, minmax(0,1fr))` }}>
            <div />
            {list.map((p, i) => (
              <div key={p.id} className="border-l-2 border-edge p-5 text-center">
                <Link to={`/ball/${p.id}`}>
                  <div className="mx-auto mb-3 aspect-square w-40 overflow-hidden rounded-2xl bg-white">
                    {p.primary_image_url && <img src={img(p.primary_image_url, 320, 320) ?? ""} alt="" className="h-full w-full object-contain p-2" />}
                  </div>
                  <div className="text-sm font-semibold uppercase tracking-wider text-mute">{p.brand_name}</div>
                  <div className="font-display text-2xl font-bold" style={{ color: COLORS[i] }}>
                    {p.name}
                  </div>
                </Link>
                <button
                  type="button"
                  onClick={() => {
                    toggleCompare(p.id);
                    navigate(`/compare?ids=${ids.filter((x) => x !== p.id).join(",")}`, { replace: true });
                  }}
                  className="mt-2 min-h-[44px] text-base font-semibold text-mute"
                >
                  ✕ Remove
                </button>
              </div>
            ))}
            {rows.map(([label, value]) => (
              <div key={label} className="contents">
                <div className="border-t-2 border-edge px-5 py-4 text-lg font-semibold text-mute">{label}</div>
                {list.map((p) => (
                  <div key={p.id} className="border-l-2 border-t-2 border-edge px-5 py-4 text-center text-lg text-ink">
                    {value(p)}
                  </div>
                ))}
              </div>
            ))}
          </div>
        </div>
        <div className="flex flex-col gap-4">
          <MiniChart points={list.map((p, i) => ({ name: p.name, color: COLORS[i], pos: byId.get(p.id) }))} />
          <p className="text-lg text-mute">
            Further right handles more oil. Higher up moves more sharply at the back end. Ask our staff how each would work with your
            game.
          </p>
          {list.length < 3 && <BigButton to="/browse" tone="ghost">+ Add another ball</BigButton>}
        </div>
      </div>
    </div>
  );
}

function MiniChart({ points }: { points: { name: string; color: string; pos?: { oil: number; motion: number } }[] }) {
  const W = 560;
  const H = 520;
  const pad = 48;
  const x = (oil: number) => pad + ((oil - 0.5) / 16) * (W - pad - 16);
  const y = (m: number) => H - pad - ((m - 0.5) / 18) * (H - pad - 16);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full rounded-3xl bg-white">
      {Array.from({ length: 16 }, (_, i) => (
        <line key={`v${i}`} x1={x(i + 1)} x2={x(i + 1)} y1={16} y2={H - pad} stroke="#eee8dc" />
      ))}
      {Array.from({ length: 18 }, (_, i) => (
        <line key={`h${i}`} y1={y(i + 1)} y2={y(i + 1)} x1={pad} x2={W - 16} stroke="#eee8dc" />
      ))}
      <text x={pad} y={H - 14} fontSize="18" fontWeight="700" fill="#0f0f2d">
        ← Light oil
      </text>
      <text x={W - 16} y={H - 14} fontSize="18" fontWeight="700" fill="#0f0f2d" textAnchor="end">
        Heavy oil →
      </text>
      <text x={20} y={H / 2} fontSize="18" fontWeight="700" fill="#0f0f2d" transform={`rotate(-90 20 ${H / 2})`} textAnchor="middle">
        Smooth ← Motion → Angular
      </text>
      {points.map((p) =>
        p.pos ? (
          <g key={p.name}>
            <circle cx={x(p.pos.oil)} cy={y(p.pos.motion)} r={16} fill={p.color} stroke="#0f0f2d" strokeWidth={2} />
            <text x={x(p.pos.oil)} y={y(p.pos.motion) - 24} fontSize="18" fontWeight="700" fill="#0f0f2d" textAnchor="middle">
              {p.name}
            </text>
          </g>
        ) : null,
      )}
    </svg>
  );
}
