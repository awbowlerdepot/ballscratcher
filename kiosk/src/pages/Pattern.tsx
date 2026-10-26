import { QRCodeSVG } from "qrcode.react";
import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { getPattern, getPatternSuggestions, type PatternDetail, type PatternSuggestions } from "../lib/api";
import { patternHandoffUrl, useKiosk } from "../lib/kiosk";
import LanePreview from "../components/LanePreview";
import { BallCard, PageTitle, Spinner } from "../components/ui";
import { stats, typeLabel } from "./Patterns";

// One oil pattern (runbook 6eu): the lane (the partner API's oil profile,
// after a burn pair), the numbers in plain words, Kegel's six track zones,
// and the balls we'd start a bowler with on it (public API
// /patterns/{id}/suggestions -- a rule of thumb from the Ball Motion Chart,
// and the page says so). A QR hands the pattern and its balls to a phone.

const ZONE_WORDS: Record<string, string> = {
  "Outside:Middle": "outside vs. middle",
  "Middle:Middle": "track vs. middle",
  "Inside:Middle": "inside vs. middle",
  "Middle:Inside": "middle vs. inside",
  "Middle:Outside": "middle vs. outside",
};

export default function Pattern() {
  const { id = "" } = useParams();
  const { location } = useKiosk();
  const [p, setP] = useState<PatternDetail | null>(null);
  const [sugg, setSugg] = useState<PatternSuggestions | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    setP(null);
    setSugg(null);
    setError(false);
    getPattern(id).then(setP).catch(() => setError(true));
    getPatternSuggestions(id).then(setSugg).catch(() => undefined);
  }, [id]);

  if (error) return <p className="text-2xl text-mute">This pattern isn't available right now.</p>;
  if (!p) return <Spinner />;

  const left = p.track_zones.slice(0, 3);
  return (
    <div className="grid grid-cols-[440px_minmax(0,1fr)] gap-10">
      <div className="flex flex-col gap-4">
        {p.profile?.rows?.length ? <LanePreview rows={p.profile.rows} /> : null}
        <p className="text-base text-dim">
          Where the oil sits on a lane after the machine has done a couple of lanes, board 1 on the left. Darker blue is more oil. Width
          isn't to scale.
        </p>
      </div>

      <div className="flex min-w-0 flex-col gap-7">
        <PageTitle
          kicker={[typeLabel(p.category), p.series].filter(Boolean).join(" · ")}
          title={p.name}
          sub={sugg?.summary}
        />

        <div className="grid grid-cols-4 gap-4">
          <Stat label="Length" value={p.length_feet != null ? `${p.length_feet} ft` : "--"} />
          <Stat label="Total oil" value={p.volume_ml != null ? `${p.volume_ml} mL` : "--"} />
          <Stat label="Ratio" value={p.ratio != null ? `${p.ratio}:1` : "--"} hint={p.ratio != null ? ratioWords(p.ratio) : undefined} />
          <Stat label="Drop brush" value={p.reverse_brush_drop_feet != null ? `${p.reverse_brush_drop_feet} ft` : "--"} />
        </div>

        {left.length > 0 && (
          <div className="rounded-3xl border-2 border-edge bg-panel p-6">
            <div className="mb-3 text-xl font-semibold text-ink">How much more oil is in the middle</div>
            <div className="grid grid-cols-3 gap-4">
              {left.map((z) => (
                <div key={`${z.from}${z.to}`}>
                  <div className="text-base text-mute">
                    Boards {z.from}–{z.to}, {ZONE_WORDS[z.label ?? ""] ?? (z.label ?? "").toLowerCase()}
                  </div>
                  <div className="font-display text-3xl font-bold text-ink">{z.ratio != null ? `${z.ratio}:1` : "--"}</div>
                </div>
              ))}
            </div>
            <p className="mt-3 text-base text-dim">Higher numbers mean more room for error; close to 1:1 means the oil is flat across the lane.</p>
          </div>
        )}

        {!sugg ? (
          <Spinner label="Finding balls…" />
        ) : (
          sugg.groups
            .filter((g) => g.balls.length)
            .map((g) => (
              <div key={g.key}>
                <div className="mb-3 flex items-baseline gap-4">
                  <h2 className="font-display text-3xl font-bold text-ink">{g.title}</h2>
                  <span className="text-lg text-mute">{g.blurb}</span>
                </div>
                <div className="grid grid-cols-4 gap-5">
                  {g.balls.map((b) => (
                    <BallCard key={b.id} ball={b} caption={b.coverstock_name ?? undefined} />
                  ))}
                </div>
              </div>
            ))
        )}
        {sugg && <p className="text-base text-dim">{sugg.note}</p>}

        <div className="flex items-center gap-6 rounded-3xl border-2 border-edge bg-panel p-6">
          <div className="rounded-2xl bg-white p-3">
            <QRCodeSVG value={patternHandoffUrl(p.id, location)} size={150} level="M" />
          </div>
          <div>
            <div className="font-display text-2xl font-bold text-ink">Take this pattern with you</div>
            <p className="mt-2 text-lg text-mute">Scan with your phone camera for the pattern and these balls. {stats(p)}.</p>
            {p.source === "kegel" && <p className="mt-2 text-base text-dim">Pattern from the Kegel Pattern Library.</p>}
          </div>
        </div>
      </div>
    </div>
  );
}

function ratioWords(r: number): string {
  return r >= 7 ? "forgiving house shot" : r >= 4 ? "medium" : "flat, sport-style";
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded-3xl border-2 border-edge bg-panel p-5">
      <div className="text-base uppercase tracking-widest text-mute">{label}</div>
      <div className="mt-1 font-display text-3xl font-bold text-ink">{value}</div>
      {hint && <div className="text-base text-mute">{hint}</div>}
    </div>
  );
}
