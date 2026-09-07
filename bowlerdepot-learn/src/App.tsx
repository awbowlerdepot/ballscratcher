import { Outlet } from "react-router-dom";
import Nav from "./components/Nav";

// Shell only, same reasoning as consumer-site/src/App.tsx -- <Outlet>
// does the actual page swap. No CompareProvider/similar here -- Learn
// has no cross-page shared state the way the ball comparison feature
// does on consumer-site.
export default function App() {
  return (
    <>
      <Nav />
      <main className="mx-auto min-h-[60vh] max-w-5xl px-6 py-10">
        <Outlet />
      </main>
      <footer className="border-t border-paper-border px-6 py-8 text-center text-sm text-muted">
        <p>
          Bowling ball reviews from{" "}
          <a href="https://bowlerdepot.com" target="_blank" rel="noreferrer" className="text-accent">
            The Bowler Depot
          </a>
          . Not affiliated with any manufacturer mentioned here.
        </p>
      </footer>
    </>
  );
}
