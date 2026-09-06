import { BrowserRouter, Route, Routes } from "react-router-dom";
import { AuthProvider, ProtectedRoute } from "./auth/AuthContext";
import Layout from "./components/Layout";
import { ToastProvider } from "./components/Toast";
import ArticlesPage from "./pages/ArticlesPage";
import BatchJobsPage from "./pages/BatchJobsPage";
import BlockedChannelsPage from "./pages/BlockedChannelsPage";
import CoresPage from "./pages/CoresPage";
import CoverstocksPage from "./pages/CoverstocksPage";
import DashboardPage from "./pages/DashboardPage";
import LoginPage from "./pages/LoginPage";
import PriceSitesPage from "./pages/PriceSitesPage";
import ProductDetailPage from "./pages/ProductDetailPage";
import ProductsPage from "./pages/ProductsPage";
import ReviewQueuePage from "./pages/ReviewQueuePage";
import VideoCandidatesPage from "./pages/VideoCandidatesPage";

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <ToastProvider>
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
            </Route>
          </Routes>
        </ToastProvider>
      </AuthProvider>
    </BrowserRouter>
  );
}
