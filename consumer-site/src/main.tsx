import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import App from "./App";
import BrowsePage from "./pages/BrowsePage";
import ProductDetailPage from "./pages/ProductDetailPage";
import ComparePage from "./pages/ComparePage";
import PlotterPage from "./pages/PlotterPage";
import "./index.css";

// Client-side route table -- every navigation inside the app swaps
// <Outlet>'s content in App.tsx without a page reload. CloudFront (see
// template.yaml's ConsumerSiteDistribution) is configured to serve
// index.html for any path that isn't a real static asset, so a direct
// load of e.g. /balls/<id> or a refresh on /compare still works.
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<App />}>
          <Route index element={<BrowsePage />} />
          <Route path="balls/:id" element={<ProductDetailPage />} />
          <Route path="compare" element={<ComparePage />} />
          <Route path="plotter" element={<PlotterPage />} />
        </Route>
      </Routes>
    </BrowserRouter>
  </StrictMode>,
);
