import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { getCurrentCards, getPattern, img, type ProductCard } from "../lib/api";
import { useKiosk } from "../lib/kiosk";

// Home (runbook 6eh): one big tile per thing a customer came to the screen
// for, plus a strip of the newest balls.
const TILES: { to: string; title: string; sub: string; icon: string; feature?: boolean }[] = [
  { to: "/find", title: "Find my ball", sub: "Answer three quick questions and we'll show you the balls that fit.", icon: "🎯", feature: true },
  { to: "/browse", title: "Browse balls", sub: "Every current ball, by brand, cover and lane condition.", icon: "🎳" },
  { to: "/next", title: "What's next for my bag", sub: "Pick the ball you throw now and see the step up, down and sideways.", icon: "➡️" },
  { to: "/replace", title: "My ball was discontinued", sub: "Find today's closest match to a retired ball.", icon: "♻️" },
  { to: "/learn", title: "Reviews & guides", sub: "Read in-depth reviews and how-to articles.", icon: "📖" },
  { to: "/new", title: "New arrivals", sub: "The latest releases from every brand.", icon: "✨" },
];

export default function Home() {
  const [newest, setNewest] = useState<ProductCard[]>([]);
  const { housePatternId } = useKiosk();
  const [house, setHouse] = useState<string | null>(null);
  useEffect(() => {
    setHouse(null);
    if (housePatternId) getPattern(housePatternId).then((p) => setHouse(p.name)).catch(() => undefined);
  }, [housePatternId]);
  useEffect(() => {
    getCurrentCards()
      .then((cards) => setNewest(cards.filter((c) => c.primary_image_url).slice(0, 6)))
      .catch(() => undefined);
  }, []);

  return (
    <div className="flex flex-col gap-5">
      <div>
        <h1 className="font-display text-5xl font-bold text-ink">What are you looking for?</h1>
        <p className="mt-2 text-2xl text-mute">Touch a tile to get started. Our staff are always happy to help, too.</p>
      </div>
      <div className="grid grid-cols-3 gap-5">
        {TILES.map((t) => (
          <Link
            key={t.to}
            to={t.to}
            className={`flex min-h-[150px] flex-col justify-between rounded-3xl border-2 px-7 py-6 transition active:scale-[0.99] ${
              t.feature ? "border-brand bg-gradient-to-br from-brand to-brand-deep" : "border-edge bg-panel active:bg-panel-2"
            }`}
          >
            <span className="text-[44px] leading-none">{t.icon}</span>
            <div>
              <div className="font-display text-3xl font-bold text-ink">{t.title}</div>
              <div className={`mt-2 text-lg ${t.feature ? "text-white/85" : "text-mute"}`}>{t.sub}</div>
            </div>
          </Link>
        ))}
      </div>
      {/* Oil Patterns (runbook 6eu): a full-width banner under the six tiles,
          so the 3x2 grid and the 1080p fit stay as they were. */}
      <Link
        to="/patterns"
        className="flex items-center justify-between gap-6 rounded-3xl border-2 border-edge bg-panel px-7 py-4 transition active:scale-[0.99] active:bg-panel-2"
      >
        <div className="flex items-center gap-5">
          <span className="text-5xl">🛢️</span>
          <div>
            <div className="font-display text-3xl font-bold text-ink">Oil patterns</div>
            <div className="mt-1 text-lg text-mute">See where the oil goes on {house ? "our house shot" : "any pattern"} and which balls fit it.</div>
          </div>
        </div>
        <span className="font-display text-2xl font-bold text-brand">{house ? `Our house shot: ${house} ›` : "Browse patterns ›"}</span>
      </Link>
      {newest.length > 0 && (
        <div>
          <div className="mb-4 flex items-baseline justify-between">
            <h2 className="font-display text-3xl font-bold text-ink">Just released</h2>
            <Link to="/new" className="text-xl font-semibold text-brand">
              See all ›
            </Link>
          </div>
          <div className="grid grid-cols-9 gap-4">
            {newest.map((b) => (
              <Link key={b.id} to={`/ball/${b.id}`} className="rounded-2xl border-2 border-edge bg-panel p-3 active:bg-panel-2">
                <div className="aspect-square overflow-hidden rounded-xl bg-white/95">
                  <img src={img(b.primary_image_url, 260, 260) ?? ""} alt="" className="h-full w-full object-contain p-2" />
                </div>
                <div className="mt-2 text-sm font-semibold uppercase tracking-wider text-mute">{b.brand_name}</div>
                <div className="font-display text-lg font-bold leading-tight text-ink">{b.name}</div>
              </Link>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
