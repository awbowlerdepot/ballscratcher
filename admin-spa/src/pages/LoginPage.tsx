import { useState, type FormEvent } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import Button from "../components/Button";

export default function LoginPage() {
  const { user, signIn } = useAuth();
  const location = useLocation();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  if (user) {
    const from = (location.state as { from?: Location })?.from?.pathname ?? "/";
    return <Navigate to={from} replace />;
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await signIn(email, password);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign-in failed.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-ink-50">
      <form onSubmit={handleSubmit} className="w-full max-w-sm rounded-lg border border-ink-200 bg-ink-100 p-6 shadow-sm">
        <h1 className="mb-1 text-lg font-semibold text-ink-800">BowlerIQ Admin</h1>
        <p className="mb-5 text-sm text-ink-500">Sign in with your admin account.</p>

        {error && <div className="mb-4 rounded-md bg-danger-light px-3 py-2 text-sm text-danger">{error}</div>}

        <label className="mb-1 block text-xs font-medium text-ink-600">Email</label>
        <input
          type="email"
          required
          autoComplete="username"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          className="mb-3 w-full rounded-md border border-ink-300 px-3 py-2 text-sm focus:border-primary focus:outline-none"
        />

        <label className="mb-1 block text-xs font-medium text-ink-600">Password</label>
        <input
          type="password"
          required
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          className="mb-5 w-full rounded-md border border-ink-300 px-3 py-2 text-sm focus:border-primary focus:outline-none"
        />

        <Button type="submit" variant="primary" className="w-full justify-center" disabled={submitting}>
          {submitting ? "Signing in…" : "Sign in"}
        </Button>
      </form>
    </div>
  );
}
