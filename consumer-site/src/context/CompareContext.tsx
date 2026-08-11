import { createContext, useContext, type ReactNode } from "react";
import { useCompareList, MAX_COMPARE_IDS } from "../hooks/useCompareList";

type CompareContextValue = ReturnType<typeof useCompareList>;

const CompareContext = createContext<CompareContextValue | null>(null);

export function CompareProvider({ children }: { children: ReactNode }) {
  const value = useCompareList();
  return <CompareContext.Provider value={value}>{children}</CompareContext.Provider>;
}

// One shared compare-list instance for the whole app (Nav's badge, the
// add/remove buttons on Browse and Product Detail, and the Compare page
// itself all need to agree on the same state) -- see useCompareList's
// own docstring for why this lives in localStorage rather than being
// re-derived from the URL on every page.
export function useCompare() {
  const ctx = useContext(CompareContext);
  if (!ctx) throw new Error("useCompare must be used within a CompareProvider");
  return ctx;
}

export { MAX_COMPARE_IDS };
