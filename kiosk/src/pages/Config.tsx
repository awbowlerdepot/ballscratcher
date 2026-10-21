import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useKiosk } from "../lib/kiosk";
import { BigButton, Keyboard, SearchField } from "../components/ui";

// Hidden kiosk config (runbook 6eh), opened by holding the logo for 3
// seconds. Al: "these will be installed at locations and knowing which is
// which is helpful. so maybe a config screen that you can set the
// location". PIN-gated (VITE_KIOSK_CONFIG_PIN at build time) so a customer
// who finds it can't change anything. The PIN is in the built JavaScript,
// so it keeps curious customers out rather than securing anything -- which
// is all this screen needs: it only sets a label.
const PIN = (import.meta.env.VITE_KIOSK_CONFIG_PIN as string | undefined) || "2468";
const BUILT = (import.meta.env.VITE_BUILD_TIME as string | undefined) || "dev";

export default function Config() {
  const navigate = useNavigate();
  const { location, locationFromUrl, deviceId, setLocation } = useKiosk();
  const [entered, setEnteredState] = useState("");
  // Latest entry, so quick taps before a re-render don't drop a digit.
  const enteredRef = useRef("");
  const setEntered = (v: string) => {
    enteredRef.current = v;
    setEnteredState(v);
  };
  const [unlocked, setUnlocked] = useState(false);
  const [draft, setDraft] = useState(location ?? "");
  const [saved, setSaved] = useState(false);

  if (!unlocked) {
    const press = (d: string) => {
      const next = (enteredRef.current + d).slice(0, 8);
      setEntered(next);
      if (next === PIN) setUnlocked(true);
      else if (next.length >= PIN.length) window.setTimeout(() => setEntered(""), 400);
    };
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-8 bg-night text-ink">
        <div className="font-display text-4xl font-bold">Kiosk settings</div>
        <div className="flex gap-3">
          {Array.from({ length: PIN.length }, (_, i) => (
            <span key={i} className={`h-5 w-5 rounded-full ${i < entered.length ? "bg-brand" : "bg-panel-2"}`} />
          ))}
        </div>
        <div className="grid grid-cols-3 gap-4">
          {["1", "2", "3", "4", "5", "6", "7", "8", "9", "", "0", "⌫"].map((d, i) =>
            d === "" ? (
              <span key={i} />
            ) : (
              <button
                key={i}
                type="button"
                onClick={() => (d === "⌫" ? setEntered(enteredRef.current.slice(0, -1)) : press(d))}
                className="h-24 w-24 rounded-2xl border-2 border-edge bg-panel font-display text-4xl font-bold active:bg-brand"
              >
                {d}
              </button>
            ),
          )}
        </div>
        <BigButton tone="ghost" onClick={() => navigate("/")}>
          Cancel
        </BigButton>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-night px-16 py-12 text-ink">
      <div className="mx-auto flex max-w-6xl flex-col gap-8">
        <div className="flex items-center justify-between">
          <h1 className="font-display text-5xl font-bold">Kiosk settings</h1>
          <BigButton onClick={() => navigate("/")}>Done</BigButton>
        </div>
        <div className="rounded-3xl border-2 border-edge bg-panel p-8">
          <div className="mb-2 text-xl font-semibold">Location</div>
          <p className="mb-4 text-lg text-mute">
            Which store or spot this kiosk is in, e.g. "Main St pro shop". It's shown in the corner and tags QR scans.
          </p>
          {locationFromUrl ? (
            <p className="text-xl text-gold">
              Set by the kiosk's start URL (<span className="font-mono">?location={location}</span>). Change it in the Windows kiosk setup.
            </p>
          ) : (
            <div className="grid grid-cols-[minmax(0,1fr)_auto] items-start gap-8">
              <div className="flex flex-col gap-4">
                <SearchField value={draft} placeholder="Location name" />
                <div className="flex gap-4">
                  <BigButton
                    onClick={() => {
                      setLocation(draft);
                      setSaved(true);
                    }}
                  >
                    Save location
                  </BigButton>
                  {saved && <span className="self-center text-xl text-[#7ee2b8]">Saved.</span>}
                </div>
              </div>
              <Keyboard
                value={draft}
                lowercase={false}
                onChange={(v) => {
                  setDraft(v);
                  setSaved(false);
                }}
              />
            </div>
          )}
        </div>
        <div className="grid grid-cols-3 gap-6">
          <Info label="Device ID" value={deviceId} />
          <Info label="Current location" value={location ?? "not set"} />
          <Info label="App build" value={BUILT} />
        </div>
        <div className="flex gap-4">
          <BigButton tone="ghost" onClick={() => window.location.reload()}>
            ↻ Reload app
          </BigButton>
        </div>
      </div>
    </div>
  );
}

function Info({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-3xl border-2 border-edge bg-panel p-6">
      <div className="text-base uppercase tracking-widest text-mute">{label}</div>
      <div className="mt-1 font-mono text-2xl">{value}</div>
    </div>
  );
}
