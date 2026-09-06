import { BrowserRouter, Route, Routes } from "react-router-dom";
import { AuthProvider, ProtectedRoute } from "./auth/AuthContext";
import Layout from "./components/Layout";
import { ToastProvider } from "./components/Toast";
import DashboardPage from "./pages/DashboardPage";
import LoginPage from "./pages/LoginPage";
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
              <Route path="/review-queue" element={<ReviewQueuePage />} />
              <Route path="/video-candidates" element={<VideoCandidatesPage />} />
            </Route>
          </Routes>
        </ToastProvider>
      </AuthProvider>
    </BrowserRouter>
  );
}
