import { useCallback, useEffect, useState } from "react";

// Al's ask: "an intuitive way to populate a ball comparison page" --
// this hook is the "populate" half. Persisted to localStorage (this is
// a real deployed site the visitor owns in their own browser, not a
// Claude.ai artifact sandbox, so localStorage is the right tool here)
// so a visitor can browse around, add a few balls to compare, and have
// the selection survive a page navigation without keeping everything
// crammed into the URL as the only source of truth. The compare PAGE
// itself still syncs ?ids= in the URL (see ComparePage) so a compare set
// is still shareable via a link -- this hook is what BrowsePage/
// ProductDetailPage use to add/remove without needing to be on the
// compare page at all.
const STORAGE_KEY = "bbd_compare_ids";
export const MAX_COMPARE_IDS = 6; // must match public_api.MAX_COMPARE_IDS

function readStored(): string[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter((v) => typeof v === "string") : [];
  } catch {
    return [];
  }
}

function writeStored(ids: string[]) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(ids));
  } catch {
    // Storage unavailable (private browsing, quota, etc.) -- the
    // in-memory state still works for the current tab, it just won't
    // survive a reload. Not worth surfacing as an error to the visitor.
  }
}

export function useCompareList() {
  const [ids, setIds] = useState<string[]>(() => readStored());

  useEffect(() => {
    writeStored(ids);
  }, [ids]);

  // Cross-tab sync -- a visitor adding a ball in one tab shouldn't leave
  // another tab's compare bar showing a stale count.
  useEffect(() => {
    function onStorage(e: StorageEvent) {
      if (e.key === STORAGE_KEY) setIds(readStored());
    }
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  const add = useCallback((id: string) => {
    setIds((prev) => (prev.includes(id) || prev.length >= MAX_COMPARE_IDS ? prev : [...prev, id]));
  }, []);

  const remove = useCallback((id: string) => {
    setIds((prev) => prev.filter((x) => x !== id));
  }, []);

  const toggle = useCallback((id: string) => {
    setIds((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : prev.length >= MAX_COMPARE_IDS ? prev : [...prev, id],
    );
  }, []);

  const clear = useCallback(() => setIds([]), []);

  const setAll = useCallback((next: string[]) => {
    setIds(next.slice(0, MAX_COMPARE_IDS));
  }, []);

  return { ids, add, remove, toggle, clear, setAll, isFull: ids.length >= MAX_COMPARE_IDS };
}
