import type { ReactNode } from "react";

// Mirrors admin-site/index.html's .badge/.badge.ok/.badge.pending/
// .badge.rejected/.badge.muted classes -- same four-color vocabulary,
// just as Tailwind utility classes instead of hand-written CSS.
type Tone = "ok" | "pending" | "danger" | "muted" | "primary";

const TONE_CLASSES: Record<Tone, string> = {
  ok: "bg-ok-light text-ok",
  pending: "bg-warn-light text-warn",
  danger: "bg-danger-light text-danger",
  // ink-200, not ink-100 -- ink-100 is the same tone as the surrounding
  // card/row surface, so a "muted" badge would be nearly invisible sitting
  // on top of one.
  muted: "bg-ink-200 text-ink-600",
  primary: "bg-primary-light text-primary-dark",
};

export default function Badge({ tone = "muted", children }: { tone?: Tone; children: ReactNode }) {
  return (
    <span className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${TONE_CLASSES[tone]}`}>
      {children}
    </span>
  );
}
