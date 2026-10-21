import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { getCurrentCards, img, type ProductCard } from "../lib/api";

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
  useEffect(() => {
    getCurrentCards()
      .then((cards) => setNewest(cards.filter((c) => c.primary_image_url).slice(0, 6)))
      .catch(() => undefined);
  }, []);

  return (
    <div className="flex flex-col gap-7">
      <div>
        <h1 className="font-display text-5xl font-bold text-ink">What are you looking for?</h1>
        <p className="mt-3 text-2xl text-mute">Touch a tile to get started. Our staff are always happy to help, too.</p>
      </div>
      <div className="grid grid-cols-3 gap-6">
        {TILES.map((t) => (
          <Link
            key={t.to}
            to={t.to}
            className={`flex min-h-[190px] flex-col justify-between rounded-3xl border-2 p-7 transition active:scale-[0.99] ${
              t.feature ? "border-brand bg-gradient-to-br from-brand to-brand-deep" : "border-edge bg-panel active:bg-panel-2"
            }`}
          >
            <span className="text-5xl">{t.icon}</span>
            <div>
              <div className="font-display text-3xl font-bold text-ink">{t.title}</div>
              <div className={`mt-2 text-lg ${t.feature ? "text-white/85" : "text-mute"}`}>{t.sub}</div>
            </div>
          </Link>
        ))}
      </div>
      {newest.length > 0 && (
        <div>
          <div className="mb-4 flex items-baseline justify-between">
            <h2 className="font-display text-3xl font-bold text-ink">Just released</h2>
            <Link to="/new" className="text-xl font-semibold text-brand">
              See all ›
            </Link>
          </div>
          <div className="grid grid-cols-8 gap-4">
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
