import { useEffect, useState } from "react";
import { getPlotterBall, type LearnPlotterPoint } from "../lib/api";
import { useKiosk } from "../lib/kiosk";
import BallSearch from "../components/BallSearch";
import { BigButton, PageTitle, Spinner } from "../components/ui";
import { FocusPlotter, Suggestions, usePlotterIndex } from "../components/plotterBits";

// "My ball was discontinued" (runbook 6eh): search any ball, including the
// 1,600+ retired ones, and see the closest CURRENT balls BowlerDepot sells
// (the plotter's retired-ball "closest" list, runbook 6cl) plus the steps
// around it. One of the most common pro shop questions.
export default function Replace() {
  const { sessionKey } = useKiosk();
  const [picked, setPicked] = useState<LearnPlotterPoint | null>(null);
  const [loading, setLoading] = useState(false);
  const { points, byId } = usePlotterIndex();
  useEffect(() => setPicked(null), [sessionKey]);

  if (picked && points) {
    const retired = picked.status === "retired";
    return (
      <div className="flex flex-col gap-8">
        <div className="flex items-start justify-between gap-6">
          <PageTitle
            kicker={retired ? "Replacing a retired ball" : "This ball is still current"}
            title={`${picked.brand_name} ${picked.name}`}
            sub={
              retired
                ? "These current balls sit closest to where it was on the ball motion chart."
                : "Good news: it's still made. Here's what's around it if you want something a little different."
            }
          />
          <BigButton tone="ghost" onClick={() => setPicked(null)}>
            ↺ Search again
          </BigButton>
        </div>
        <Suggestions ball={picked} byId={byId} roles={retired ? ["closest"] : ["twins"]} />
        <FocusPlotter ball={picked} points={points} byId={byId} />
        <Suggestions ball={picked} byId={byId} roles={["more_oil", "less_oil", "more_angular", "smoother"]} title="Or a step in any direction" />
      </div>
    );
  }

  return (
    <div>
      <PageTitle kicker="Discontinued ball?" title="Find today's closest match" sub="Type the name of the ball you used to throw." />
      {loading ? (
        <Spinner />
      ) : (
        <BallSearch
          placeholder="e.g. Hammer Black Widow"
          onPick={(item) => {
            setLoading(true);
            getPlotterBall(item.id)
              .then(setPicked)
              .finally(() => setLoading(false));
          }}
        />
      )}
    </div>
  );
}
