import { useEffect, useState } from "react";

// Desktop (lg, ≥1024px) gets the full-width chart with a floating details
// card (runbook 6ce/6ci); smaller screens stack the details under the
// chart. Shared by the /plotter page and the article panel.
const DESKTOP_QUERY = "(min-width: 1024px)";

export function useIsDesktop() {
  const [desktop, setDesktop] = useState(() => typeof window !== "undefined" && window.matchMedia(DESKTOP_QUERY).matches);
  useEffect(() => {
    const mq = window.matchMedia(DESKTOP_QUERY);
    const onChange = () => setDesktop(mq.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);
  return desktop;
}
