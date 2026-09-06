import { useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import Badge from "./Badge";
import Button from "./Button";
import ErrorBoundary from "./ErrorBoundary";
import {
  IconArticles,
  IconBatch,
  IconBlocked,
  IconClose,
  IconCore,
  IconCoverstock,
  IconDashboard,
  IconMenu,
  IconPanelLeft,
  IconPriceTag,
  IconProducts,
  IconReviewQueue,
  IconVideo,
} from "./icons";

const NAV_ITEMS = [
  { to: "/", label: "Dashboard", end: true, icon: IconDashboard },
  { to: "/products", label: "Products", end: false, icon: IconProducts },
  { to: "/review-queue", label: "Review Queue", end: false, icon: IconReviewQueue },
  { to: "/video-candidates", label: "Video Candidates", end: false, icon: IconVideo },
  { to: "/articles", label: "Articles", end: false, icon: IconArticles },
  { to: "/price-sites", label: "Price Sites", end: false, icon: IconPriceTag },
  { to: "/cores", label: "Cores", end: false, icon: IconCore },
  { to: "/coverstocks", label: "Coverstocks", end: false, icon: IconCoverstock },
  { to: "/blocked-channels", label: "Blocked Channels", end: false, icon: IconBlocked },
  { to: "/batch-jobs", label: "Batch Jobs", end: false, icon: IconBatch },
];

// Persisted across reloads/sessions -- a "dense pro-tool" reviewer
// living in this app all day will want the wider table area back once
// they've learned the icons, not to re-collapse it every time.
const SIDEBAR_COLLAPSED_KEY = "admin-spa:sidebar-collapsed";

// Shell: sidebar + top bar + <Outlet/>. Every top-level admin-site tab
// has now been ported here (Batch Jobs, the last one, landed
// 2026-09-05) -- admin-site/index.html's richer per-product detail
// sub-view (Videos section, rescan, bulk reassign/delete) has not,
// though, so that file still has a real reason to stick around. See
// README.md's "What's not here yet".
//
// Mobile pass (2026-09-05, Al: "the last few tabs done... now I want
// to work on making this a bit more mobile friendly"): below the `md`
// breakpoint the sidebar stops being a permanently-reserved column
// (previously just `<div className="flex ...">` with no responsive
// behavior at all -- on a ~375px phone that meant the icon rail alone
// ate ~15% of width, or the expanded 56-column ate well over half) and
// becomes an off-canvas overlay drawer instead: fixed-position,
// translated fully off-screen by default, slid in via `mobileOpen`
// (triggered by a hamburger button in the header, closed by the
// backdrop, an explicit close button, or picking a nav item). The
// desktop collapse/expand icon-rail behavior (`collapsed` state) is
// untouched at md+ and simply doesn't apply below it -- two separate
// buttons (one `hidden md:inline-flex`, one `md:hidden`) avoid needing
// a JS matchMedia check to decide which behavior a click should have.
export default function Layout() {
  const { user, signOut } = useAuth();
  const location = useLocation();
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === "1");
  const [mobileOpen, setMobileOpen] = useState(false);

  function toggleCollapsed() {
    const next = !collapsed;
    setCollapsed(next);
    localStorage.setItem(SIDEBAR_COLLAPSED_KEY, next ? "1" : "0");
  }

  return (
    <div className="flex min-h-screen">
      {/* Backdrop -- mobile only, sits between the drawer (z-40) and
          page content, tapping it closes the drawer same as a nav pick. */}
      {mobileOpen && (
        <div
          className="fixed inset-0 z-30 bg-black/60 md:hidden"
          onClick={() => setMobileOpen(false)}
          aria-hidden="true"
        />
      )}
      <aside
        className={`fixed inset-y-0 left-0 z-40 w-64 shrink-0 border-r border-ink-200 bg-ink-100 transition-transform duration-150 md:static md:z-auto md:translate-x-0 md:transition-[width] ${
          mobileOpen ? "translate-x-0" : "-translate-x-full"
        } ${collapsed ? "md:w-14" : "md:w-56"}`}
      >
        <div
          className={`flex items-center gap-2 border-b border-ink-200 py-4 ${collapsed ? "md:justify-center md:px-2" : "md:px-4"} px-4`}
        >
          {/* Desktop collapse/expand toggle -- hidden on mobile, where
              the drawer is all-or-nothing (see IconClose below). */}
          <button
            onClick={toggleCollapsed}
            title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            className="hidden shrink-0 rounded-md p-1 text-ink-500 hover:bg-ink-200 hover:text-ink-800 md:inline-flex"
          >
            <IconPanelLeft className="h-5 w-5" />
          </button>
          <span className={`truncate text-base font-semibold text-ink-800 ${collapsed ? "md:hidden" : ""}`}>
            BowlerIQ Admin
          </span>
          {/* Mobile drawer's own close button -- pushed to the far
              right via ml-auto since there's no collapse toggle to its
              left on mobile. */}
          <button
            onClick={() => setMobileOpen(false)}
            aria-label="Close menu"
            className="ml-auto shrink-0 rounded-md p-1 text-ink-500 hover:bg-ink-200 hover:text-ink-800 md:hidden"
          >
            <IconClose className="h-5 w-5" />
          </button>
        </div>
        <nav className="flex flex-col gap-0.5 p-2">
          {NAV_ITEMS.map((item) => {
            const Icon = item.icon;
            return (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                title={collapsed ? item.label : undefined}
                onClick={() => setMobileOpen(false)}
                className={({ isActive }) =>
                  `flex items-center gap-2.5 rounded-md py-2 text-sm font-medium px-3 ${
                    collapsed ? "md:justify-center md:px-2" : ""
                  } ${isActive ? "bg-primary-light text-primary-dark" : "text-ink-600 hover:bg-ink-200"}`
                }
              >
                <Icon className="h-5 w-5 shrink-0" />
                <span className={collapsed ? "md:hidden" : ""}>{item.label}</span>
              </NavLink>
            );
          })}
        </nav>
      </aside>
      <div className="flex flex-1 flex-col">
        <header className="flex items-center justify-between gap-3 border-b border-ink-200 bg-ink-100 px-4 py-3 sm:px-6">
          <button
            onClick={() => setMobileOpen(true)}
            aria-label="Open menu"
            className="shrink-0 rounded-md p-1 text-ink-500 hover:bg-ink-200 hover:text-ink-800 md:hidden"
          >
            <IconMenu className="h-5 w-5" />
          </button>
          <div className="flex flex-1 flex-wrap items-center justify-end gap-2">
            {user?.role === null && (
              <Badge tone="danger">No role assigned -- ask an admin to add you to a Cognito group</Badge>
            )}
            {user?.role && <Badge tone={user.role === "admin" ? "primary" : "muted"}>{user.role}</Badge>}
            {/* Hidden below sm -- role badge + Sign out are the two
                things worth keeping visible at all times on a phone;
                the email is a nice-to-have that just eats width there. */}
            <span className="hidden text-sm text-ink-600 sm:inline">{user?.email}</span>
            <Button size="sm" variant="ghost" onClick={signOut}>
              Sign out
            </Button>
          </div>
        </header>
        <main className="flex-1 bg-ink-50 p-4 sm:p-6">
          {/* Keyed by pathname so navigating to a different page resets a
              caught error -- the class component itself won't naturally
              remount just because the route changed. */}
          <ErrorBoundary key={location.pathname}>
            <Outlet />
          </ErrorBoundary>
        </main>
      </div>
    </div>
  );
}
