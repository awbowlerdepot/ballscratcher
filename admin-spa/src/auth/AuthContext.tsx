import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { type AuthUser, getCurrentUser, signIn as cognitoSignIn, signOut as cognitoSignOut } from "./cognito";

interface AuthContextValue {
  user: AuthUser | null;
  // true only while the initial getCurrentUser() check is in flight on
  // app load -- distinct from "no user", so ProtectedRoute doesn't
  // flash a redirect-to-login before a real (still-valid) session has
  // had a chance to load.
  loading: boolean;
  signIn: (email: string, password: string) => Promise<void>;
  signOut: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    getCurrentUser()
      .then(setUser)
      .finally(() => setLoading(false));
  }, []);

  async function signIn(email: string, password: string) {
    const signedInUser = await cognitoSignIn(email, password);
    setUser(signedInUser);
  }

  function signOut() {
    cognitoSignOut();
    setUser(null);
  }

  return <AuthContext.Provider value={{ user, loading, signIn, signOut }}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within an AuthProvider");
  return ctx;
}

// Wraps any route that requires a signed-in user. Note this is a UX
// gate only, not a security boundary -- the real enforcement is
// server-side in admin_api_authorizer (a signed-out user simply can't
// get a valid ID token to send). A signed-in user with role=null
// (authenticated but not in Admins/Editors) is let through here so they
// can at least see why every API call is failing, rather than being
// bounced straight back to the login form with no explanation.
export function ProtectedRoute({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth();
  const location = useLocation();

  if (loading) {
    return <div className="flex h-screen items-center justify-center text-ink-500">Loading…</div>;
  }
  if (!user) {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }
  return <>{children}</>;
}

// Wraps a route that's Admins-only (currently just /users -- Al: "can we
// add user management and a user group that has no access to user
// managment"). Unlike ProtectedRoute above, this one IS worth being
// strict about even though it's still not the real security boundary
// (admin_api's require_admin_role/403 is -- an Editor who edits the URL
// bar straight to /users and somehow got past this would still get
// nothing but 403s from every API call the page makes). Redirects
// Editors and role=null accounts to the Dashboard rather than showing a
// page that would just error on every request.
export function AdminRoute({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth();

  if (loading) {
    return <div className="flex h-screen items-center justify-center text-ink-500">Loading…</div>;
  }
  if (user?.role !== "admin") {
    return <Navigate to="/" replace />;
  }
  return <>{children}</>;
}
