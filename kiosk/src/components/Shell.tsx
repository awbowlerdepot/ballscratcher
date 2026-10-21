import { useRef, type ReactNode } from "react";
import { Link, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useIdleReset, useKiosk } from "../lib/kiosk";

// Kiosk frame (runbook 6eh): a fixed top bar (logo, Back, Home, compare
// tray) over full-height content. Long-pressing the logo for 3 seconds
// opens the hidden config screen (location, device id); a quick tap goes
// home. The idle reset runs on every page inside this shell.
// "The Bowler Depot" mark (no ".com" -- Al: "can we use the bowler depot
// logo not the .com version"), rendered white from Al's print file
// (thebowlerdepot-for-print.ai) into public/.
const LOGO = "/bowlerdepot-logo-white.png";
const LONG_PRESS_MS = 3000;

export default function Shell() {
  const navigate = useNavigate();
  const route = useLocation();
  const { compare, location } = useKiosk();
  useIdleReset(false);
  const pressTimer = useRef<number | undefined>(undefined);
  const longPressed = useRef(false);

  const startPress = () => {
    longPressed.current = false;
    pressTimer.current = window.setTimeout(() => {
      longPressed.current = true;
      navigate("/config");
    }, LONG_PRESS_MS);
  };
  const endPress = () => {
    window.clearTimeout(pressTimer.current);
    if (!longPressed.current) navigate("/home");
  };

  const isHome = route.pathname === "/home";
  return (
    <div className="flex min-h-screen flex-col bg-night text-ink">
      <header className="sticky top-0 z-40 flex h-24 items-center gap-4 border-b-2 border-edge bg-night/95 px-8 backdrop-blur">
        <button
          type="button"
          onPointerDown={startPress}
          onPointerUp={endPress}
          onPointerLeave={() => window.clearTimeout(pressTimer.current)}
          onContextMenu={(e) => e.preventDefault()}
          className="flex h-16 items-center"
          aria-label="BowlerDepot home"
        >
          <img src={LOGO} alt="BowlerDepot" className="h-14 w-auto" draggable={false} />
        </button>
        <div className="ml-4 flex gap-3">
          {!isHome && (
            <NavButton onClick={() => navigate(-1)}>
              <span className="text-2xl">‹</span> Back
            </NavButton>
          )}
          {!isHome && <NavButton onClick={() => navigate("/home")}>⌂ Home</NavButton>}
        </div>
        <div className="ml-auto flex items-center gap-4">
          {location && <span className="text-sm uppercase tracking-widest text-dim">{location}</span>}
          <Link
            to={compare.length ? `/compare?ids=${compare.join(",")}` : "/browse"}
            className={`flex min-h-[64px] items-center gap-3 rounded-2xl px-6 text-xl font-semibold ${
              compare.length ? "bg-brand text-white" : "border-2 border-edge text-mute"
            }`}
          >
            Compare
            <span className={`rounded-full px-3 py-0.5 text-lg ${compare.length ? "bg-white/25" : "bg-panel-2"}`}>{compare.length}/3</span>
          </Link>
        </div>
      </header>
      <main className="flex-1 px-12 py-10">
        <Outlet />
      </main>
    </div>
  );
}

function NavButton({ children, onClick }: { children: ReactNode; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex min-h-[64px] items-center gap-2 rounded-2xl border-2 border-edge px-6 text-xl font-semibold text-ink active:bg-panel-2"
    >
      {children}
    </button>
  );
}
