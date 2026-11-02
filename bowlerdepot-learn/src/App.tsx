import { Outlet } from "react-router-dom";
import Footer from "./components/Footer";
import Nav from "./components/Nav";
import ScrollToTop from "./components/ScrollToTop";

// Shell only, same reasoning as consumer-site/src/App.tsx -- <Outlet>
// does the actual page swap. No CompareProvider/similar here -- Learn
// has no cross-page shared state the way the ball comparison feature
// does on consumer-site.
export default function App() {
  return (
    <>
      <ScrollToTop />
      <Nav />
      <main id="main-content" className="mx-auto min-h-[60vh] max-w-[75rem] px-4 py-8 md:px-6 md:py-10">
        <Outlet />
      </main>
      <Footer />
    </>
  );
}
