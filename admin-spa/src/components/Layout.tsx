import { NavLink, Outlet, useLocation } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import Badge from "./Badge";
import Button from "./Button";
import ErrorBoundary from "./ErrorBoundary";

const NAV_ITEMS = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/products", label: "Products", end: false },
  { to: "/review-queue", label: "Review Queue", end: false },
  { to: "/video-candidates", label: "Video Candidates", end: false },
];

// Shell: sidebar + top bar + <Outlet/>. Articles, Price Sites, Cores,
// Coverstocks, Blocked Channels, and Batch Jobs still live on the
// existing admin-site/index.html for now (see README.md's "not here
// yet" section) -- tabs move over here one at a time as they're
// ported.
export default function Layout() {
  const { user, signOut } = useAuth();
  const location = useLocation();

  return (
    <div className="flex min-h-screen">
      <aside className="w-56 shrink-0 border-r border-slate-200 bg-white">
        <div className="border-b border-slate-200 px-4 py-4">
          <span className="text-base font-semibold text-slate-800">BowlerIQ Admin</span>
        </div>
        <nav className="flex flex-col gap-0.5 p-2">
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) =>
                `rounded-md px-3 py-2 text-sm font-medium ${
                  isActive ? "bg-primary-light text-primary-dark" : "text-slate-600 hover:bg-slate-100"
                }`
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
      </aside>
      <div className="flex flex-1 flex-col">
        <header className="flex items-center justify-between border-b border-slate-200 bg-white px-6 py-3">
          <div />
          <div className="flex items-center gap-3">
            {user?.role === null && (
              <Badge tone="danger">No role assigned -- ask an admin to add you to a Cognito group</Badge>
            )}
            {user?.role && <Badge tone={user.role === "admin" ? "primary" : "muted"}>{user.role}</Badge>}
            <span className="text-sm text-slate-600">{user?.email}</span>
            <Button size="sm" variant="ghost" onClick={signOut}>
              Sign out
            </Button>
          </div>
        </header>
        <main className="flex-1 bg-slate-50 p-6">
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
