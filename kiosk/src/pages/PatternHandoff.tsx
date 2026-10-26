import { useEffect, useState, type ReactNode } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { getPattern, getPatternSuggestions, img, type PatternDetail, type PatternSuggestions } from "../lib/api";
import { stats } from "./Patterns";

// Phone hand-off for an oil pattern (runbook 6eu): what the QR on a kiosk
// pattern page opens on a customer's phone. Like the ball hand-off (/m/:id)
// it's a light mobile page outside the kiosk shell; each suggested ball
// links to its own hand-off page, which carries the store tag on to
// bowlerdepot.com and Learn.
export default function PatternHandoff() {
  const { id = "" } = useParams();
  const [params] = useSearchParams();
  const from = params.get("from");
  const [p, setP] = useState<PatternDetail | null>(null);
  const [sugg, setSugg] = useState<PatternSuggestions | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    document.title = "Oil pattern | BowlerDepot";
    getPattern(id).then(setP).catch(() => setError(true));
    getPatternSuggestions(id).then(setSugg).catch(() => undefined);
  }, [id]);

  if (error) return <Page><p className="p-6 text-lg">This pattern isn't available right now.</p></Page>;
  if (!p) return <Page><p className="p-6 text-lg text-[#6b6b63]">Loading…</p></Page>;
  const q = from ? `?from=${encodeURIComponent(from)}` : "";

  return (
    <Page>
      <div className="bg-white px-5 pb-5 pt-5 text-center">
        <div className="text-sm font-semibold uppercase tracking-widest text-[#1f439e]">Oil pattern</div>
        <h1 className="font-display text-3xl font-bold text-[#0f0f2d]">{p.name}</h1>
        <p className="mt-1 text-base text-[#6b6b63]">{stats(p)}</p>
        {sugg && <p className="mt-3 text-base text-[#0f0f2d]">{sugg.summary}</p>}
      </div>
      <div className="flex flex-col gap-5 p-5">
        {sugg?.groups
          .filter((g) => g.balls.length)
          .map((g) => (
            <div key={g.key}>
              <div className="text-lg font-semibold">{g.title}</div>
              <div className="mb-2 text-sm text-[#6b6b63]">{g.blurb}</div>
              <div className="flex flex-col gap-2">
                {g.balls.map((b) => (
                  <Link key={b.id} to={`/m/${b.id}${q}`} className="flex items-center gap-3 rounded-2xl border border-[#e8e2d6] bg-white p-3">
                    <div className="h-14 w-14 shrink-0 overflow-hidden rounded-xl">
                      {b.primary_image_url && <img src={img(b.primary_image_url, 120, 120) ?? ""} alt="" className="h-full w-full object-contain" />}
                    </div>
                    <div>
                      <div className="text-xs font-semibold uppercase tracking-wider text-[#1f439e]">{b.brand_name}</div>
                      <div className="font-semibold">{b.name} ›</div>
                    </div>
                  </Link>
                ))}
              </div>
            </div>
          ))}
        {sugg && <p className="text-sm text-[#6b6b63]">{sugg.note}</p>}
        {p.source === "kegel" && p.source_url && (
          <a href={p.source_url} className="rounded-2xl border border-[#e8e2d6] bg-white px-5 py-4">
            <div className="text-lg font-semibold">The full pattern sheet ›</div>
            <div className="text-sm text-[#6b6b63]">On the Kegel Pattern Library</div>
          </a>
        )}
        <p className="mt-2 text-center text-sm text-[#6b6b63]">Questions? Our pro shop staff are happy to help.</p>
      </div>
    </Page>
  );
}

function Page({ children }: { children: ReactNode }) {
  return (
    <div className="selectable min-h-screen bg-[#faf8f4] font-sans text-[#0f0f2d]">
      <div className="flex justify-center border-b border-[#e8e2d6] bg-white py-3">
        <img src="/bowlerdepot-logo-dark.png" alt="The Bowler Depot" className="h-9 w-auto" />
      </div>
      <div className="mx-auto max-w-md">{children}</div>
    </div>
  );
}
