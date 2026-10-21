import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { getPlotterBall, type LearnPlotterPoint } from "../lib/api";
import { useKiosk } from "../lib/kiosk";
import BallSearch from "../components/BallSearch";
import { BigButton, PageTitle, Spinner } from "../components/ui";
import { FocusPlotter, Suggestions, usePlotterIndex } from "../components/plotterBits";

// "What's next for my bag" (runbook 6eh): pick the ball you throw now and
// see the nearest step toward more oil, less oil, more angular and smoother
// (the plotter's directional suggestions) -- how pro shops build an arsenal.
export default function NextStep() {
  const { sessionKey } = useKiosk();
  const [picked, setPicked] = useState<LearnPlotterPoint | null>(null);
  const [loading, setLoading] = useState(false);
  const { points, byId } = usePlotterIndex();
  useEffect(() => setPicked(null), [sessionKey]);

  if (picked && points) {
    return (
      <div className="flex flex-col gap-8">
        <div className="flex items-start justify-between gap-6">
          <PageTitle
            kicker="Building your bag"
            title={`From your ${picked.name}`}
            sub="Each group is the nearest ball in that direction. A good bag has a clear step between balls."
          />
          <div className="flex gap-4">
            <BigButton tone="ghost" to={`/ball/${picked.id}`}>
              Open {picked.name}
            </BigButton>
            <BigButton tone="ghost" onClick={() => setPicked(null)}>
              ↺ Pick another
            </BigButton>
          </div>
        </div>
        <Suggestions ball={picked} byId={byId} roles={["more_oil", "less_oil", "more_angular", "smoother"]} />
        <FocusPlotter ball={picked} points={points} byId={byId} />
        <Suggestions ball={picked} byId={byId} roles={picked.status === "retired" ? ["closest"] : ["twins"]} title="Close to it" />
        <p className="text-xl text-mute">
          Not sure which direction you need? <Link to="/find" className="font-semibold text-brand">Answer three questions</Link> or ask our staff.
        </p>
      </div>
    );
  }

  return (
    <div>
      <PageTitle kicker="What's next for my bag" title="Which ball do you throw now?" sub="Type its name. Retired balls work too." />
      {loading ? (
        <Spinner />
      ) : (
        <BallSearch
          placeholder="e.g. Storm Phaze II"
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
