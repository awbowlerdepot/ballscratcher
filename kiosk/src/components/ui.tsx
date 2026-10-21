import { QRCodeSVG } from "qrcode.react";
import { useEffect, useRef, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { img } from "../lib/api";
import { handoffUrl, useKiosk } from "../lib/kiosk";

// Shared kiosk UI (runbook 6eh). Everything is sized for a 27" landscape
// touchscreen: 56px+ touch targets, large type, no hover-only affordances.

export function Spinner({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-3 py-20 text-xl text-mute">
      <span className="h-6 w-6 animate-spin rounded-full border-4 border-edge border-t-brand" />
      {label}
    </div>
  );
}

export function PageTitle({ kicker, title, sub }: { kicker?: string; title: string; sub?: string }) {
  return (
    <div className="mb-8">
      {kicker && <div className="mb-2 text-sm font-semibold uppercase tracking-[0.2em] text-brand">{kicker}</div>}
      <h1 className="font-display text-5xl font-bold leading-tight text-ink">{title}</h1>
      {sub && <p className="mt-3 max-w-4xl text-xl text-mute">{sub}</p>}
    </div>
  );
}

export function BigButton({
  children,
  onClick,
  to,
  tone = "brand",
  className = "",
  disabled,
}: {
  children: ReactNode;
  onClick?: () => void;
  to?: string;
  tone?: "brand" | "ghost" | "hot";
  className?: string;
  disabled?: boolean;
}) {
  const tones = {
    brand: "bg-brand text-white active:bg-brand-deep",
    ghost: "border-2 border-edge bg-panel text-ink active:bg-panel-2",
    hot: "bg-hot text-white active:opacity-80",
  };
  const cls = `inline-flex min-h-[64px] items-center justify-center gap-3 rounded-2xl px-8 text-xl font-semibold transition disabled:opacity-40 ${tones[tone]} ${className}`;
  if (to) {
    return (
      <Link to={to} className={cls}>
        {children}
      </Link>
    );
  }
  return (
    <button type="button" onClick={onClick} disabled={disabled} className={cls}>
      {children}
    </button>
  );
}

export function Chip({ active, onClick, children }: { active: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`min-h-[52px] rounded-full border-2 px-6 text-lg font-semibold transition ${
        active ? "border-brand bg-brand text-white" : "border-edge bg-panel text-mute active:bg-panel-2"
      }`}
    >
      {children}
    </button>
  );
}

export interface CardBall {
  id: string;
  name: string;
  brand_name: string;
  primary_image_url?: string | null;
}

export function BallCard({ ball, caption, badge }: { ball: CardBall; caption?: ReactNode; badge?: ReactNode }) {
  const { compare, toggleCompare } = useKiosk();
  const inCompare = compare.includes(ball.id);
  const src = img(ball.primary_image_url, 360, 360);
  return (
    <div className="relative flex flex-col rounded-3xl border-2 border-edge bg-panel p-4 transition active:scale-[0.99]">
      <Link to={`/ball/${ball.id}`} className="flex flex-1 flex-col">
        <div className="relative mb-3 aspect-square overflow-hidden rounded-2xl bg-white/95">
          {src ? <img src={src} alt="" className="h-full w-full object-contain p-3" loading="lazy" /> : null}
          {badge && <div className="absolute left-2 top-2">{badge}</div>}
        </div>
        <div className="text-sm font-semibold uppercase tracking-wider text-mute">{ball.brand_name}</div>
        <div className="font-display text-2xl font-bold leading-tight text-ink">{ball.name}</div>
        {caption && <div className="mt-1 text-base text-mute">{caption}</div>}
      </Link>
      <button
        type="button"
        onClick={() => toggleCompare(ball.id)}
        className={`mt-3 min-h-[48px] rounded-xl border-2 text-base font-semibold ${
          inCompare ? "border-brand bg-brand/20 text-ink" : "border-edge text-mute active:bg-panel-2"
        }`}
      >
        {inCompare ? "✓ In compare" : "+ Compare"}
      </button>
    </div>
  );
}

export function Grid({ children }: { children: ReactNode }) {
  return <div className="grid grid-cols-4 gap-6 2xl:grid-cols-5">{children}</div>;
}

// "Take it with you": a QR code for the phone hand-off page (/m/<id>), next
// to an "ask a staff member" prompt. Al: "Staff and scan qr code that opens
// ball on a kiosk specific page that has links to the public sites
// learn, ecom, etc".
export function TakeItWithYou({ ballId, name }: { ballId: string; name: string }) {
  const { location } = useKiosk();
  return (
    <div className="flex items-center gap-6 rounded-3xl border-2 border-edge bg-panel p-6">
      <div className="rounded-2xl bg-white p-3">
        <QRCodeSVG value={handoffUrl(ballId, location)} size={168} level="M" />
      </div>
      <div>
        <div className="font-display text-2xl font-bold text-ink">Take the {name} with you</div>
        <p className="mt-2 text-lg text-mute">
          Scan with your phone camera for the full review, videos and the BowlerDepot.com page.
        </p>
        <p className="mt-3 text-lg font-semibold text-gold">Ready to buy or get drilled? Ask anyone on our staff.</p>
      </div>
    </div>
  );
}

// Minimal Markdown for the AI review summaries: #/##/### headings, "- "
// bullets, **bold**, and paragraphs. No links or HTML (nothing on the
// kiosk should navigate away).
export function Markdown({ text }: { text: string }) {
  const blocks: ReactNode[] = [];
  let list: string[] = [];
  const inline = (s: string, key: string) =>
    s.split(/(\*\*[^*]+\*\*)/g).map((part, i) =>
      part.startsWith("**") && part.endsWith("**") ? (
        <strong key={`${key}-${i}`} className="text-ink">
          {part.slice(2, -2)}
        </strong>
      ) : (
        <span key={`${key}-${i}`}>{part.replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")}</span>
      ),
    );
  const flush = () => {
    if (list.length) {
      const items = list;
      blocks.push(
        <ul key={`ul-${blocks.length}`} className="mb-5 list-disc space-y-2 pl-7">
          {items.map((li, i) => (
            <li key={i}>{inline(li, `li-${blocks.length}-${i}`)}</li>
          ))}
        </ul>,
      );
      list = [];
    }
  };
  text.split(/\n/).forEach((raw, i) => {
    const line = raw.trim();
    if (!line) return flush();
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) {
      flush();
      blocks.push(
        <h3 key={`h-${i}`} className="mb-3 mt-6 font-display text-2xl font-bold text-ink first:mt-0">
          {h[2]}
        </h3>,
      );
      return;
    }
    const b = line.match(/^[-*]\s+(.*)$/);
    if (b) {
      list.push(b[1]);
      return;
    }
    flush();
    blocks.push(
      <p key={`p-${i}`} className="mb-4">
        {inline(line, `p-${i}`)}
      </p>,
    );
  });
  flush();
  return <div className="text-xl leading-relaxed text-mute">{blocks}</div>;
}

// Full-screen YouTube player. youtube-nocookie, no related videos from other
// channels, no fullscreen button or YouTube logo link -- the kiosk can't
// leave the app, and the Edge allowlist blocks youtube.com navigation anyway.
export function VideoModal({ videoId, title, onClose }: { videoId: string; title: string; onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const src = `https://www.youtube-nocookie.com/embed/${videoId}?autoplay=1&rel=0&modestbranding=1&fs=0&playsinline=1&iv_load_policy=3&disablekb=1`;
  return (
    <div className="fixed inset-0 z-50 flex flex-col bg-black/95 p-8">
      <div className="mb-4 flex items-center justify-between gap-6">
        <div className="truncate font-display text-2xl font-bold text-ink">{title}</div>
        <BigButton tone="ghost" onClick={onClose}>
          ✕ Close
        </BigButton>
      </div>
      <div className="relative flex-1 overflow-hidden rounded-2xl">
        <iframe
          src={src}
          title={title}
          className="absolute inset-0 h-full w-full"
          allow="autoplay; encrypted-media; picture-in-picture"
          referrerPolicy="strict-origin-when-cross-origin"
        />
      </div>
    </div>
  );
}

// On-screen keyboard: the kiosk has no physical keyboard, and relying on
// Windows' touch keyboard inside Edge kiosk mode is unreliable.
const ROWS = ["1234567890", "QWERTYUIOP", "ASDFGHJKL", "ZXCVBNM"];
export function Keyboard({
  value,
  onChange,
  onDone,
  lowercase = true,
}: {
  value: string;
  onChange: (v: string) => void;
  onDone?: () => void;
  lowercase?: boolean; // search fields type lowercase; names (the config location) keep capitals
}) {
  // Build each press on the latest text, not this render's `value`: two
  // quick taps before a re-render would otherwise drop a letter.
  const latest = useRef(value);
  latest.current = value;
  const set = (next: string) => {
    latest.current = next;
    onChange(next);
  };
  const key = (label: ReactNode, onPress: () => void, wide = "") => (
    <button
      type="button"
      onClick={onPress}
      className={`min-h-[64px] rounded-xl border-2 border-edge bg-panel-2 px-2 font-display text-2xl font-semibold text-ink active:bg-brand ${wide || "w-[72px]"}`}
    >
      {label}
    </button>
  );
  return (
    <div className="flex flex-col items-center gap-2 rounded-3xl border-2 border-edge bg-panel p-4">
      {ROWS.map((row) => (
        <div key={row} className="flex gap-2">
          {row.split("").map((ch) => (
            <span key={ch}>{key(ch, () => set(latest.current + (lowercase ? ch.toLowerCase() : ch)))}</span>
          ))}
          {row === "ZXCVBNM" && key("⌫", () => set(latest.current.slice(0, -1)), "w-[120px]")}
        </div>
      ))}
      <div className="flex gap-2">
        {key("-", () => set(latest.current + "-"))}
        {key("Space", () => set(latest.current + " "), "w-[420px]")}
        {key("Clear", () => set(""), "w-[140px]")}
        {onDone && key("Done", onDone, "w-[140px] !bg-brand")}
      </div>
    </div>
  );
}

export function SearchField({ value, placeholder }: { value: string; placeholder: string }) {
  return (
    <div className="flex min-h-[72px] items-center rounded-2xl border-2 border-brand bg-panel px-6 text-3xl">
      {value ? <span className="text-ink">{value}</span> : <span className="text-dim">{placeholder}</span>}
      <span className="ml-1 inline-block h-9 w-[3px] animate-pulse bg-brand" />
    </div>
  );
}
