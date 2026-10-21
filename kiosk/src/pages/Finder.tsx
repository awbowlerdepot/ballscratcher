import { useEffect, useMemo, useState } from "react";
import { getCurrentCards, getPlotter, type LearnPlotterPoint, type ProductCard } from "../lib/api";
import { useKiosk } from "../lib/kiosk";
import { BallCard, BigButton, Grid, PageTitle, Spinner } from "../components/ui";

// "Find my ball" (runbook 6eh). Three big-button questions mapped onto the
// ball motion plotter's two axes -- oil (1 light .. 16 heavy) and motion
// (1 smooth .. 18 angular) -- plus a cover preference. Results are the
// current balls BowlerDepot sells (plotter `recommendable`, the same rule
// the Learn plotter's suggestions use), nearest to the answer first.

type Range = [number, number] | null;
interface Option {
  label: string;
  sub: string;
  range?: Range;
  cover?: string | null;
}

const QUESTIONS: { key: "oil" | "motion" | "cover"; title: string; options: Option[] }[] = [
  {
    key: "oil",
    title: "What are your lanes usually like?",
    options: [
      { label: "Dry or broken down", sub: "The ball hooks early; lanes get dry fast.", range: [1, 6.5] },
      { label: "A typical house shot", sub: "Medium oil, the league-night normal.", range: [6.5, 11] },
      { label: "Fresh or heavy oil", sub: "The ball skids and doesn't want to hook.", range: [11, 16] },
      { label: "Not sure", sub: "Show me balls for any condition.", range: null },
    ],
  },
  {
    key: "motion",
    title: "How do you want it to hook?",
    options: [
      { label: "Smooth and predictable", sub: "An even, controllable arc.", range: [1, 8.5] },
      { label: "Strong and continuous", sub: "Reads early and keeps rolling.", range: [8.5, 13] },
      { label: "Sharp at the back end", sub: "Saves energy, then changes direction hard.", range: [13, 18] },
      { label: "Not sure", sub: "Any shape is fine.", range: null },
    ],
  },
  {
    key: "cover",
    title: "Any preference on the cover?",
    options: [
      { label: "Solid", sub: "More grip in oil, an earlier, smoother read.", cover: "solid" },
      { label: "Pearl", sub: "More length and a stronger back end.", cover: "pearl" },
      { label: "Hybrid", sub: "In between: length with control.", cover: "hybrid" },
      { label: "No preference", sub: "Show me everything.", cover: null },
    ],
  },
];

const mid = (r: Range, fallback: number) => (r ? (r[0] + r[1]) / 2 : fallback);
const inRange = (v: number, r: Range, slack = 0) => !r || (v >= r[0] - slack && v <= r[1] + slack);

export default function Finder() {
  const { sessionKey } = useKiosk();
  const [step, setStep] = useState(0);
  const [answers, setAnswers] = useState<Option[]>([]);
  const [points, setPoints] = useState<LearnPlotterPoint[] | null>(null);
  const [cards, setCards] = useState<Map<string, ProductCard>>(new Map());

  useEffect(() => {
    setStep(0);
    setAnswers([]);
  }, [sessionKey]);

  useEffect(() => {
    getPlotter().then(setPoints).catch(() => setPoints([]));
    getCurrentCards()
      .then((cs) => setCards(new Map(cs.map((c) => [c.id, c]))))
      .catch(() => undefined);
  }, []);

  const done = step >= QUESTIONS.length;
  const results = useMemo(() => {
    if (!done || !points) return [];
    const [oil, motion, cover] = answers;
    const target = { oil: mid(oil.range ?? null, 8.5), motion: mid(motion.range ?? null, 9.5) };
    const pick = (slack: number) =>
      points
        .filter((p) => p.recommendable && p.status !== "retired")
        .filter((p) => inRange(p.oil, oil.range ?? null, slack) && inRange(p.motion, motion.range ?? null, slack))
        .filter((p) => !cover.cover || cards.get(p.id)?.coverstock_type === cover.cover)
        .map((p) => ({ p, d: Math.hypot(p.oil - target.oil, (p.motion - target.motion) * 0.8) }))
        .sort((a, b) => a.d - b.d);
    // Widen once if the exact answer is too narrow to show a useful list.
    const strict = pick(0);
    return (strict.length >= 4 ? strict : pick(1.5)).slice(0, 12).map((x) => x.p);
  }, [done, points, answers, cards]);

  if (!done) {
    const q = QUESTIONS[step];
    return (
      <div className="mx-auto max-w-6xl">
        <div className="mb-6 flex gap-2">
          {QUESTIONS.map((_, n) => (
            <span key={n} className={`h-2 flex-1 rounded-full ${n <= step ? "bg-brand" : "bg-panel-2"}`} />
          ))}
        </div>
        <PageTitle kicker={`Question ${step + 1} of ${QUESTIONS.length}`} title={q.title} />
        <div className="grid grid-cols-2 gap-6">
          {q.options.map((o) => (
            <button
              key={o.label}
              type="button"
              onClick={() => {
                setAnswers([...answers.slice(0, step), o]);
                setStep(step + 1);
              }}
              className="flex min-h-[180px] flex-col justify-center rounded-3xl border-2 border-edge bg-panel p-8 text-left active:border-brand active:bg-panel-2"
            >
              <span className="font-display text-4xl font-bold text-ink">{o.label}</span>
              <span className="mt-3 text-xl text-mute">{o.sub}</span>
            </button>
          ))}
        </div>
        {step > 0 && (
          <div className="mt-8">
            <BigButton tone="ghost" onClick={() => setStep(step - 1)}>
              ‹ Previous question
            </BigButton>
          </div>
        )}
      </div>
    );
  }

  return (
    <div>
      <PageTitle
        kicker="Your matches"
        title={results.length ? `${results.length} balls that fit` : "No exact matches"}
        sub={answers.map((a) => a.label).join(" · ")}
      />
      <div className="mb-8 flex gap-4">
        <BigButton
          tone="ghost"
          onClick={() => {
            setStep(0);
            setAnswers([]);
          }}
        >
          ↺ Start over
        </BigButton>
      </div>
      {!points ? (
        <Spinner />
      ) : results.length ? (
        <Grid>
          {results.map((p) => (
            <BallCard
              key={p.id}
              ball={p}
              caption={`${cards.get(p.id)?.coverstock_type ? cards.get(p.id)!.coverstock_type + " · " : ""}oil ${p.oil.toFixed(0)} · motion ${p.motion.toFixed(0)}`}
            />
          ))}
        </Grid>
      ) : (
        <p className="text-2xl text-mute">Try "Not sure" on one of the questions, or ask our staff for a recommendation.</p>
      )}
    </div>
  );
}
