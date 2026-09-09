import { lazy, Suspense } from "react";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { AdminRoute, AuthProvider, ProtectedRoute } from "./auth/AuthContext";
import Layout from "./components/Layout";
import { ToastProvider } from "./components/Toast";
import LoginPage from "./pages/LoginPage";

// Al: "admin spa is slow to load... what can we do to speed that up" --
// every page below used to be a plain top-of-file import, so the very
// first load shipped ALL twelve pages' code in one bundle regardless of
// which route was actually visited -- including recharts + chart.js +
// react-chartjs-2 (only used by DashboardPage and ProductDetailPage's
// charts), which are large enough on their own to matter. React.lazy +
// Suspense splits each page into its own chunk that's only fetched the
// first time its route is actually navigated to. LoginPage stays a plain
// import -- it's the very first thing an unauthenticated visitor sees,
// so lazy-loading it would trade one network round trip (chunk fetch)
// for a Suspense flash on the one page where that's most visible, for a
// component small enough that splitting it out isn't worth much anyway.
const DashboardPage = lazy(() => import("./pages/DashboardPage"));
const ProductsPage = lazy(() => import("./pages/ProductsPage"));
const ProductDetailPage = lazy(() => import("./pages/ProductDetailPage"));
const ReviewQueuePage = lazy(() => import("./pages/ReviewQueuePage"));
const VideoCandidatesPage = lazy(() => import("./pages/VideoCandidatesPage"));
const ArticlesPage = lazy(() => import("./pages/ArticlesPage"));
const PriceSitesPage = lazy(() => import("./pages/PriceSitesPage"));
const CoresPage = lazy(() => import("./pages/CoresPage"));
const CoverstocksPage = lazy(() => import("./pages/CoverstocksPage"));
const BlockedChannelsPage = lazy(() => import("./pages/BlockedChannelsPage"));
const BatchJobsPage = lazy(() => import("./pages/BatchJobsPage"));
const UsersPage = lazy(() => import("./pages/UsersPage"));

// Deliberately plain and generic -- covers only the brief window while a
// route's own JS chunk downloads (typically much faster than the
// admin_api call that page then makes), not that page's own data-loading
// state. Each page already has its own DataTable-loading/Dashboard/
// ProductDetail skeleton (see components/Skeleton.tsx and friends) for
// that; this fallback almost never has time to become visible on a warm
// cache.
function RouteFallback() {
  return <div className="flex h-screen items-center justify-center text-ink-500">Loading…</div>;
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <ToastProvider>
          <Suspense fallback={<RouteFallback />}>
            <Routes>
              <Route path="/login" element={<LoginPage />} />
              <Route
                element={
                  <ProtectedRoute>
                    <Layout />
                  </ProtectedRoute>
                }
              >
                <Route path="/" element={<DashboardPage />} />
                <Route path="/products" element={<ProductsPage />} />
                <Route path="/products/:id" element={<ProductDetailPage />} />
                <Route path="/review-queue" element={<ReviewQueuePage />} />
                <Route path="/video-candidates" element={<VideoCandidatesPage />} />
                <Route path="/articles" element={<ArticlesPage />} />
                <Route path="/price-sites" element={<PriceSitesPage />} />
                <Route path="/cores" element={<CoresPage />} />
                <Route path="/coverstocks" element={<CoverstocksPage />} />
                <Route path="/blocked-channels" element={<BlockedChannelsPage />} />
                <Route path="/batch-jobs" element={<BatchJobsPage />} />
                <Route
                  path="/users"
                  element={
                    <AdminRoute>
                      <UsersPage />
                    </AdminRoute>
                  }
                />
              </Route>
            </Routes>
          </Suspense>
        </ToastProvider>
      </AuthProvider>
    </BrowserRouter>
  );
}
