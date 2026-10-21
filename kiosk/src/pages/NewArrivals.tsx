import { useEffect, useState } from "react";
import { getCurrentCards, type ProductCard } from "../lib/api";
import { BallCard, Grid, PageTitle, Spinner } from "../components/ui";

// New arrivals (runbook 6eh): current balls by release date, newest first.
export default function NewArrivals() {
  const [cards, setCards] = useState<ProductCard[] | null>(null);
  useEffect(() => {
    getCurrentCards()
      .then((cs) => setCards(cs.filter((c) => c.release_date).slice(0, 24)))
      .catch(() => setCards([]));
  }, []);
  const fmt = (d: string) => new Date(d + "T00:00:00").toLocaleDateString(undefined, { month: "long", day: "numeric", year: "numeric" });
  return (
    <div>
      <PageTitle kicker="New arrivals" title="The latest releases" sub="Newest first, from every brand we carry." />
      {!cards ? (
        <Spinner />
      ) : (
        <Grid>
          {cards.map((c) => {
            const upcoming = c.release_date! > new Date().toISOString().slice(0, 10);
            return (
              <BallCard
                key={c.id}
                ball={c}
                caption={`${upcoming ? "Coming" : "Released"} ${fmt(c.release_date!)}`}
                badge={upcoming ? <span className="rounded-full bg-gold px-3 py-1 text-sm font-bold text-night">Coming soon</span> : undefined}
              />
            );
          })}
        </Grid>
      )}
    </div>
  );
}
