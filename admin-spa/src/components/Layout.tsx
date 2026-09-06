import { useState } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import Badge from "./Badge";
import Button from "./Button";
import ErrorBoundary from "./ErrorBoundary";
import {
  IconArticles,
  IconChevronsLeft,
  IconChevronsRight,
  IconCore,
  IconDashboard,
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
];

// Persisted across reloads/sessions -- a "dense pro-tool" reviewer
// living in this app all day will want the wider table area back once
// they've learned the icons, not to re-collapse it every time.
const SIDEBAR_COLLAPSED_KEY = "admin-spa:sidebar-collapsed";

// Shell: sidebar + top bar + <Outlet/>. Coverstocks, Blocked Channels,
// and Batch Jobs still live on the existing admin-site/index.html for
// now (see README.md's "not here yet" section) -- tabs move over here
// one at a time as they're ported.
export default function Layout() {
  const { user, signOut } = useAuth();
  const location = useLocation();
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === "1");

  function toggleCollapsed() {
    const next = !collapsed;
    setCollapsed(next);
    localStorage.setItem(SIDEBAR_COLLAPSED_KEY, next ? "1" : "0");
  }

  return (
    <div className="flex min-h-screen">
      <aside
        className={`${collapsed ? "w-14" : "w-56"} shrink-0 border-r border-ink-200 bg-ink-100 transition-[width] duration-150`}
      >
        <div
          className={`flex items-center border-b border-ink-200 py-4 ${collapsed ? "justify-center px-2" : "px-4"}`}
        >
          {!collapsed && <span className="text-base font-semibold text-ink-800">BowlerIQ Admin</span>}
        </div>
        <nav className="flex flex-col gap-0.5 p-2">
          {/* Styled as a nav row, not a corner icon button, so it reads
              as part of the menu (same icon size, same left-aligned
              layout, same hover treatment) rather than chrome bolted
              onto the header. */}
          <button
            onClick={toggleCollapsed}
            title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            className={`flex items-center gap-2.5 rounded-md py-2 text-sm font-medium text-ink-600 hover:bg-ink-200 ${
              collapsed ? "justify-center px-2" : "px-3"
            }`}
          >
            {collapsed ? <IconChevronsRight className="h-5 w-5 shrink-0" /> : <IconChevronsLeft className="h-5 w-5 shrink-0" />}
            {!collapsed && <span>Collapse</span>}
          </button>
          {NAV_ITEMS.map((item) => {
            const Icon = item.icon;
            return (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.end}
                title={collapsed ? item.label : undefined}
                className={({ isActive }) =>
                  `flex items-center gap-2.5 rounded-md py-2 text-sm font-medium ${
                    collapsed ? "justify-center px-2" : "px-3"
                  } ${isActive ? "bg-primary-light text-primary-dark" : "text-ink-600 hover:bg-ink-200"}`
                }
              >
                <Icon className="h-5 w-5 shrink-0" />
                {!collapsed && <span>{item.label}</span>}
              </NavLink>
            );
          })}
        </nav>
      </aside>
      <div className="flex flex-1 flex-col">
        <header className="flex items-center justify-between border-b border-ink-200 bg-ink-100 px-6 py-3">
          <div />
          <div className="flex items-center gap-3">
            {user?.role === null && (
              <Badge tone="danger">No role assigned -- ask an admin to add you to a Cognito group</Badge>
            )}
            {user?.role && <Badge tone={user.role === "admin" ? "primary" : "muted"}>{user.role}</Badge>}
            <span className="text-sm text-ink-600">{user?.email}</span>
            <Button size="sm" variant="ghost" onClick={signOut}>
              Sign out
            </Button>
          </div>
        </header>
        <main className="flex-1 bg-ink-50 p-6">
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
