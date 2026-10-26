import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { getPattern, getPatterns, type PatternDetail, type PatternSummary } from "../lib/api";
import { useKiosk } from "../lib/kiosk";
import { BigButton, Chip, Keyboard, PageTitle, SearchField, Spinner } from "../components/ui";

// Oil Patterns (runbook 6eu). Al: "can we add an oil pattern section to the
// kiosk... also do you think we can suggest balls for each pattern" and
// "add the per location house shot". Every approved pattern (the Kegel
// Pattern Library plus sheets we've uploaded), filtered by type, length and
// oil, searchable with the on-screen keyboard. This location's house shot
// (set on the config screen) is featured first. Filters reset with the
// session.

const TYPES: { key: string; label: string }[] = [
  // Kegel files almost everything as sport, so "house" also means a
  // house-style ratio (7:1 or more), the same line the suggestions use.
  { key: "house", label: "House shots" },
  { key: "sport", label: "Sport" },
  { key: "challenge", label: "Challenge" },
  { key: "tournament", label: "Tournament" },
  { key: "recreational", label: "Recreational" },
];
// Feet / mL bands, matching the words the ball suggestions use.
const LENGTHS: { key: string; label: string; test: (ft: number) => boolean }[] = [
  { key: "short", label: "Short (38 ft or less)", test: (ft) => ft <= 38 },
  { key: "medium", label: "Medium (39–44 ft)", test: (ft) => ft > 38 && ft < 45 },
  { key: "long", label: "Long (45 ft+)", test: (ft) => ft >= 45 },
];
const OILS: { key: string; label: string; test: (ml: number) => boolean }[] = [
  { key: "light", label: "Light oil", test: (ml) => ml < 22 },
  { key: "medium", label: "Medium oil", test: (ml) => ml >= 22 && ml <= 28 },
  { key: "heavy", label: "Heavy oil", test: (ml) => ml > 28 },
];
const PAGE = 48;

export default function Patterns() {
  const { sessionKey, housePatternId } = useKiosk();
  const [all, setAll] = useState<PatternSummary[] | null>(null);
  const [house, setHouse] = useState<PatternDetail | null>(null);
  const [type, setType] = useState<string | null>(null);
  const [length, setLength] = useState<string | null>(null);
  const [oil, setOil] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [typing, setTyping] = useState(false);
  const [shown, setShown] = useState(PAGE);

  useEffect(() => {
    setType(null);
    setLength(null);
    setOil(null);
    setQ("");
    setTyping(false);
    setShown(PAGE);
  }, [sessionKey]);

  useEffect(() => {
    getPatterns()
      .then(setAll)
      .catch(() => setAll([]));
  }, []);
  useEffect(() => {
    setHouse(null);
    if (housePatternId) getPattern(housePatternId).then(setHouse).catch(() => undefined);
  }, [housePatternId]);

  const list = useMemo(() => {
    const words = q.trim().toLowerCase().split(/\s+/).filter(Boolean);
    const lb = LENGTHS.find((b) => b.key === length);
    const ob = OILS.find((b) => b.key === oil);
    return (all ?? []).filter(
      (p) =>
        (!type || p.category === type || (type === "house" && (p.ratio ?? 0) >= 7)) &&
        (!lb || (p.length_feet != null && lb.test(p.length_feet))) &&
        (!ob || (p.volume_ml != null && ob.test(p.volume_ml))) &&
        words.every((w) => `${p.name} ${p.series ?? ""}`.toLowerCase().includes(w)),
    ).sort(newestFirst);
  }, [all, type, length, oil, q]);
  useEffect(() => setShown(PAGE), [type, length, oil, q]);

  return (
    <div>
      <PageTitle
        kicker="Oil patterns"
        title="What are the lanes like?"
        sub="Pick a pattern to see where the oil goes and which balls we'd start you with."
      />

      {house && !q && !type && !length && !oil && (
        <Link
          to={`/pattern/${house.id}`}
          className="mb-8 flex items-center justify-between gap-6 rounded-3xl border-2 border-brand bg-gradient-to-br from-brand to-brand-deep p-7 active:opacity-90"
        >
          <div>
            <div className="text-sm font-semibold uppercase tracking-[0.2em] text-white/80">Our house shot</div>
            <div className="font-display text-4xl font-bold text-white">{house.name}</div>
            <div className="mt-2 text-xl text-white/85">{stats(house)}</div>
          </div>
          <span className="shrink-0 font-display text-2xl font-bold text-white">See the oil and the balls ›</span>
        </Link>
      )}

      <div className="mb-6 flex flex-col gap-4 rounded-3xl border-2 border-edge bg-panel p-6">
        <FilterRow label="Type">
          <Chip active={!type} onClick={() => setType(null)}>
            All
          </Chip>
          {TYPES.map((t) => (
            <Chip key={t.key} active={type === t.key} onClick={() => setType(type === t.key ? null : t.key)}>
              {t.label}
            </Chip>
          ))}
        </FilterRow>
        <FilterRow label="Length">
          <Chip active={!length} onClick={() => setLength(null)}>
            Any
          </Chip>
          {LENGTHS.map((b) => (
            <Chip key={b.key} active={length === b.key} onClick={() => setLength(length === b.key ? null : b.key)}>
              {b.label}
            </Chip>
          ))}
        </FilterRow>
        <FilterRow label="Oil">
          <Chip active={!oil} onClick={() => setOil(null)}>
            Any
          </Chip>
          {OILS.map((b) => (
            <Chip key={b.key} active={oil === b.key} onClick={() => setOil(oil === b.key ? null : b.key)}>
              {b.label}
            </Chip>
          ))}
          <span className="mx-4 h-8 w-0.5 bg-edge" />
          <Chip active={typing || Boolean(q)} onClick={() => setTyping(!typing)}>
            🔎 {q ? `“${q}”` : "Search by name"}
          </Chip>
        </FilterRow>
        {typing && (
          <div className="grid grid-cols-[minmax(0,1fr)_auto] items-start gap-6">
            <SearchField value={q} placeholder="Pattern name, e.g. Cheetah or USBC" />
            <Keyboard value={q} onChange={setQ} onDone={() => setTyping(false)} />
          </div>
        )}
      </div>

      {!all ? (
        <Spinner />
      ) : list.length ? (
        <>
          <p className="mb-4 text-xl text-mute">
            {list.length} of {all.length} patterns
          </p>
          <div className="grid grid-cols-3 gap-5">
            {list.slice(0, shown).map((p) => (
              <PatternCard key={p.id} p={p} />
            ))}
          </div>
          {shown < list.length && (
            <div className="mt-8 flex justify-center">
              <BigButton tone="ghost" onClick={() => setShown(shown + PAGE)}>
                Show more ({list.length - shown} left)
              </BigButton>
            </div>
          )}
        </>
      ) : (
        <p className="text-2xl text-mute">No patterns match those filters. Try removing one.</p>
      )}
    </div>
  );
}

// Most library names start with the event's year ("2026 PBA Elite
// Regional ..."): newest year first, then A-Z; names without a year after.
function newestFirst(a: PatternSummary, b: PatternSummary): number {
  const year = (n: string) => Number(/^(19|20)\d{2}\b/.exec(n)?.[0] ?? 0);
  return year(b.name) - year(a.name) || a.name.localeCompare(b.name);
}

export function stats(p: { length_feet: number | null; volume_ml: number | null; ratio: number | null }): string {
  return [
    p.length_feet != null ? `${p.length_feet} ft` : null,
    p.volume_ml != null ? `${p.volume_ml} mL` : null,
    p.ratio != null ? `${p.ratio}:1` : null,
  ]
    .filter(Boolean)
    .join(" · ");
}

export function typeLabel(category: string): string {
  return TYPES.find((t) => t.key === category)?.label.replace(/s$/, "") ?? category;
}

function PatternCard({ p }: { p: PatternSummary }) {
  return (
    <Link to={`/pattern/${p.id}`} className="flex flex-col justify-between rounded-3xl border-2 border-edge bg-panel p-6 active:bg-panel-2">
      <div>
        <div className="text-sm font-semibold uppercase tracking-wider text-mute">
          {typeLabel(p.category)}
          {p.series ? ` · ${p.series}` : ""}
        </div>
        <div className="mt-1 font-display text-2xl font-bold leading-tight text-ink">{p.name}</div>
      </div>
      <div className="mt-4 text-xl font-semibold text-brand">{stats(p)}</div>
    </Link>
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
