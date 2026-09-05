import type { ReactNode } from "react";

// Mirrors admin-site/index.html's .badge/.badge.ok/.badge.pending/
// .badge.rejected/.badge.muted classes -- same four-color vocabulary,
// just as Tailwind utility classes instead of hand-written CSS.
type Tone = "ok" | "pending" | "danger" | "muted" | "primary";

const TONE_CLASSES: Record<Tone, string> = {
  ok: "bg-ok-light text-green-800",
  pending: "bg-warn-light text-warn",
  danger: "bg-danger-light text-red-800",
  muted: "bg-slate-100 text-slate-500",
  primary: "bg-primary-light text-primary-dark",
};

export default function Badge({ tone = "muted", children }: { tone?: Tone; children: ReactNode }) {
  return (
    <span className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${TONE_CLASSES[tone]}`}>
      {children}
    </span>
  );
}
