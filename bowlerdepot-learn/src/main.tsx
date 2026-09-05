import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import App from "./App";
import LearnIndexPage from "./pages/LearnIndexPage";
import ArticleDetailPage from "./pages/ArticleDetailPage";
import "./index.css";

// Client-side route table -- same SPA shape as consumer-site/src/
// main.tsx. CloudFront (see template.yaml's LearnSiteDistribution, task
// #441) serves index.html for any non-asset path so a direct load of
// /articles/<product-id> or a refresh on it still works client-side.
// Note this SAME app is also what scripts/prerender.ts (task #440)
// renders to static HTML at build time for real crawlable per-article
// pages -- see that script's own header comment for why both exist.
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<App />}>
          <Route index element={<LearnIndexPage />} />
          <Route path="articles/:productId" element={<ArticleDetailPage />} />
        </Route>
      </Routes>
    </BrowserRouter>
  </StrictMode>,
);
