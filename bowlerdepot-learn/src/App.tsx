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
      <main className="site-main">
        <Outlet />
      </main>
      <footer className="site-footer">
        <p>
          Bowling ball reviews from{" "}
          <a href="https://bowlerdepot.com" target="_blank" rel="noreferrer">
            The Bowler Depot
          </a>
          . Not affiliated with any manufacturer mentioned here.
        </p>
      </footer>
    </>
  );
}
