import { lazy, StrictMode, Suspense } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import "./index.css";
import Shell from "./components/Shell";
import { Spinner } from "./components/ui";
import { KioskProvider } from "./lib/kiosk";

// BowlerDepot in-store kiosk (runbook 6eh). Al: "build a kiosk that is
// locked down to bowlerdepot.com sites ... Really it could be an entirely
// new 'kiosk' ui that is built with bowleriq properties only ... the
// experience to match instore not online". Routes:
//   /            attract loop (idle screen)
//   /home        tiles
//   /find        "find my ball" questions -> short list
//   /browse      filter the current catalog
//   /ball/:id    ball page (specs, plotter, reviews, videos, QR)
//   /compare     2-3 balls side by side
//   /replace     "my ball was discontinued" -> closest current balls
//   /next        "what's next for my arsenal" -> directional steps
//   /new         new arrivals
//   /learn, /learn/:slug   Learn articles
//   /m/:id       phone hand-off page (opened from the QR; outside the shell)
//   /config      hidden location/device screen (long-press the logo)
const Attract = lazy(() => import("./pages/Attract"));
const Home = lazy(() => import("./pages/Home"));
const Finder = lazy(() => import("./pages/Finder"));
const Browse = lazy(() => import("./pages/Browse"));
const Ball = lazy(() => import("./pages/Ball"));
const Compare = lazy(() => import("./pages/Compare"));
const Replace = lazy(() => import("./pages/Replace"));
const NextStep = lazy(() => import("./pages/NextStep"));
const NewArrivals = lazy(() => import("./pages/NewArrivals"));
const LearnList = lazy(() => import("./pages/LearnList"));
const LearnArticle = lazy(() => import("./pages/LearnArticle"));
const Handoff = lazy(() => import("./pages/Handoff"));
const Config = lazy(() => import("./pages/Config"));

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <KioskProvider>
        <Suspense fallback={<div className="min-h-screen bg-night"><Spinner /></div>}>
          <Routes>
            <Route path="/" element={<Attract />} />
            <Route path="/m/:id" element={<Handoff />} />
            <Route path="/config" element={<Config />} />
            <Route element={<Shell />}>
              <Route path="/home" element={<Home />} />
              <Route path="/find" element={<Finder />} />
              <Route path="/browse" element={<Browse />} />
              <Route path="/ball/:id" element={<Ball />} />
              <Route path="/compare" element={<Compare />} />
              <Route path="/replace" element={<Replace />} />
              <Route path="/next" element={<NextStep />} />
              <Route path="/new" element={<NewArrivals />} />
              <Route path="/learn" element={<LearnList />} />
              <Route path="/learn/:slug" element={<LearnArticle />} />
              <Route path="*" element={<Home />} />
            </Route>
          </Routes>
        </Suspense>
      </KioskProvider>
    </BrowserRouter>
  </StrictMode>,
);
