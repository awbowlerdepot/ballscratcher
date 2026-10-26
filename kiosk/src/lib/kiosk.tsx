import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useLocation, useNavigate } from "react-router-dom";

// Kiosk-wide state (runbook 6eh).
//
// LOCATION. Al: "these will be installed at locations and knowing which is
// which is helpful. so maybe a config screen that you can set the
// location". Set on the hidden config screen (long-press the logo, then
// the PIN) and kept in localStorage, which survives because the device
// runs Edge's kiosk mode in "interactive signage" style (no InPrivate wipe;
// this app does its own idle reset). A `?location=` query parameter on the
// kiosk's start URL wins over the stored value -- the fallback for a device
// whose storage was cleared. The device id is a random id generated once
// per device, so two kiosks at one location can still be told apart.
//
// HOUSE SHOT (runbook 6eu). Al: "add the per location house shot". The oil
// pattern this location's lanes run, picked on the config screen from the
// published patterns and kept in localStorage like the location; a
// `?house=<pattern id>` on the start URL wins, the same fallback. The Oil
// Patterns section features it first.
//
// SESSION. The compare tray and anything a customer typed belong to one
// customer: after IDLE_MS without a touch the app clears them and returns
// to the attract loop, so the next person starts fresh. The phone hand-off
// pages (/m/...) and the config screen are excluded from the idle reset.

const LOCATION_KEY = "kiosk:location";
const DEVICE_KEY = "kiosk:device-id";
const HOUSE_KEY = "kiosk:house-pattern";
export const IDLE_MS = 90_000;
export const MAX_COMPARE = 3;

function safeGet(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function safeSet(key: string, value: string | null) {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    // storage unavailable: the URL parameter still works
  }
}

function deviceId(): string {
  let id = safeGet(DEVICE_KEY);
  if (!id) {
    id = (crypto.randomUUID?.() ?? Math.random().toString(36).slice(2)).slice(0, 8);
    safeSet(DEVICE_KEY, id);
  }
  return id;
}

interface KioskState {
  location: string | null;
  locationFromUrl: boolean;
  deviceId: string;
  setLocation: (value: string | null) => void;
  housePatternId: string | null;
  houseFromUrl: boolean;
  setHousePattern: (id: string | null) => void;
  compare: string[];
  toggleCompare: (id: string) => void;
  clearCompare: () => void;
  sessionKey: number; // bumps on every reset, so pages can drop local state
  resetSession: () => void;
}

const Ctx = createContext<KioskState | null>(null);

export function KioskProvider({ children }: { children: ReactNode }) {
  const urlLocation = useMemo(() => new URLSearchParams(window.location.search).get("location"), []);
  const [stored, setStored] = useState<string | null>(() => safeGet(LOCATION_KEY));
  const urlHouse = useMemo(() => new URLSearchParams(window.location.search).get("house"), []);
  const [storedHouse, setStoredHouse] = useState<string | null>(() => safeGet(HOUSE_KEY));
  const [compare, setCompare] = useState<string[]>([]);
  const [sessionKey, setSessionKey] = useState(0);
  const id = useMemo(deviceId, []);

  const setLocation = useCallback((value: string | null) => {
    const v = value?.trim() || null;
    safeSet(LOCATION_KEY, v);
    setStored(v);
  }, []);

  const setHousePattern = useCallback((id: string | null) => {
    safeSet(HOUSE_KEY, id);
    setStoredHouse(id);
  }, []);

  const toggleCompare = useCallback((ballId: string) => {
    setCompare((cur) => (cur.includes(ballId) ? cur.filter((x) => x !== ballId) : cur.length >= MAX_COMPARE ? cur : [...cur, ballId]));
  }, []);

  const clearCompare = useCallback(() => setCompare([]), []);
  // Stable identity: useIdleReset depends on it.
  const resetSession = useCallback(() => {
    setCompare([]);
    setSessionKey((k) => k + 1);
  }, []);

  const value: KioskState = {
    location: urlLocation || stored,
    locationFromUrl: Boolean(urlLocation),
    deviceId: id,
    setLocation,
    housePatternId: urlHouse || storedHouse,
    houseFromUrl: Boolean(urlHouse),
    setHousePattern,
    compare,
    toggleCompare,
    clearCompare,
    sessionKey,
    resetSession,
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useKiosk() {
  const v = useContext(Ctx);
  if (!v) throw new Error("useKiosk outside KioskProvider");
  return v;
}

// Returns to the attract loop after IDLE_MS without input, clearing the
// session. Any touch, click, key or scroll counts as activity.
export function useIdleReset(disabled: boolean) {
  const navigate = useNavigate();
  const route = useLocation();
  const { resetSession } = useKiosk();
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => {
    if (disabled || route.pathname === "/") return;
    const arm = () => {
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => {
        resetSession();
        navigate("/", { replace: true });
        window.scrollTo(0, 0);
      }, IDLE_MS);
    };
    const events = ["pointerdown", "keydown", "wheel", "touchmove"] as const;
    events.forEach((e) => window.addEventListener(e, arm, { passive: true }));
    arm();
    return () => {
      window.clearTimeout(timer.current);
      events.forEach((e) => window.removeEventListener(e, arm));
    };
  }, [disabled, route.pathname, navigate, resetSession]);
}

// Builds the QR hand-off URL for a ball: a phone-friendly page on this same
// site with links out to Learn, bowlerdepot.com and the rest. The location
// rides along so the phone page's outbound links can be tagged per store.
export function patternHandoffUrl(patternId: string, location: string | null): string {
  const url = new URL(`/m/pattern/${patternId}`, window.location.origin);
  if (location) url.searchParams.set("from", location);
  return url.toString();
}

export function handoffUrl(ballId: string, location: string | null): string {
  const url = new URL(`/m/${ballId}`, window.location.origin);
  if (location) url.searchParams.set("from", location);
  return url.toString();
}
