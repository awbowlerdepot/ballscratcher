import type { SVGProps } from "react";

// Hand-rolled instead of pulling in an icon library (lucide-react etc.)
// -- admin-spa has no icon dependency yet, this sandbox's npm registry
// access is unreliable (see DEPLOY_RUNBOOK.md's restyle writeup), and
// seven nav icons plus a collapse toggle is a small enough set that a
// dependency isn't worth it. Stroke-based, `currentColor`, matching
// Tabler/Feather's line-icon proportions so they read consistently with
// the rest of the app's flat style -- color follows whatever text color
// class the parent (nav item, button) already sets.
type IconProps = SVGProps<SVGSVGElement>;

const base = {
  viewBox: "0 0 20 20",
  fill: "none" as const,
  stroke: "currentColor",
  strokeWidth: 1.6,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
};

export function IconDashboard(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <rect x="2.5" y="2.5" width="6.5" height="6.5" rx="1.2" />
      <rect x="11" y="2.5" width="6.5" height="6.5" rx="1.2" />
      <rect x="2.5" y="11" width="6.5" height="6.5" rx="1.2" />
      <rect x="11" y="11" width="6.5" height="6.5" rx="1.2" />
    </svg>
  );
}

export function IconProducts(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <path d="M10 2.3 17 6.2v7.6L10 17.7 3 13.8V6.2z" />
      <path d="M3 6.2 10 10.1l7-3.9" />
      <path d="M10 10.1v7.6" />
    </svg>
  );
}

export function IconReviewQueue(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <rect x="4" y="2.8" width="12" height="14.5" rx="1.5" />
      <path d="M7.5 2.8v-.6a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1v.6" />
      <path d="M6.8 9.5 8.3 11l3.9-4" />
      <path d="M6.8 14h6.4" />
    </svg>
  );
}

export function IconVideo(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <circle cx="10" cy="10" r="7.3" />
      <path d="M8.3 7.1v5.8l5.1-2.9z" fill="currentColor" stroke="none" />
    </svg>
  );
}

export function IconArticles(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <path d="M5.3 2.5h6.4L15.3 6v11.5H5.3z" />
      <path d="M11.7 2.5V6h3.6" />
      <path d="M7.3 10h5.4M7.3 12.5h5.4M7.3 15h3.4" />
    </svg>
  );
}

export function IconPriceTag(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <path d="M10.6 2.5h5a1 1 0 0 1 1 1v5L9.2 16 2.5 9.3z" />
      <circle cx="13.4" cy="6.3" r="1" fill="currentColor" stroke="none" />
    </svg>
  );
}

export function IconCore(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <circle cx="10" cy="10" r="7.3" />
      <circle cx="10" cy="10" r="3.8" />
      <circle cx="10" cy="10" r="0.9" fill="currentColor" stroke="none" />
    </svg>
  );
}

export function IconCoverstock(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <path d="M10 2.6c2.4 3 5.4 6.8 5.4 9.9a5.4 5.4 0 1 1-10.8 0c0-3.1 3-6.9 5.4-9.9z" />
    </svg>
  );
}

export function IconChevronsLeft(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <path d="M12.3 4 6.8 10l5.5 6" />
    </svg>
  );
}

export function IconChevronsRight(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <path d="M7.7 4 13.2 10l-5.5 6" />
    </svg>
  );
}

// The sidebar-collapse toggle -- a rounded outer frame with a vertical
// divider near the left third, the classic "panel/sidebar" glyph (VS
// Code, Notion, etc. all use this shape) rather than a chevron, so it
// reads as "toggle a panel" at a glance instead of "go back/forward".
export function IconPanelLeft(props: IconProps) {
  return (
    <svg {...base} {...props}>
      <rect x="2.5" y="3.5" width="15" height="13" rx="2" />
      <path d="M7.8 3.5v13" />
    </svg>
  );
}
