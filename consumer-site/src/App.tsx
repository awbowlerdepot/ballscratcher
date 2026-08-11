import { Outlet } from "react-router-dom";
import Nav from "./components/Nav";
import { CompareProvider } from "./context/CompareContext";

// Shell only -- react-router's <Outlet> is what actually swaps page
// content on navigation (see main.tsx for the route table), which is
// what keeps this a real single-page app rather than a multi-page site
// that happens to share a nav bar.
export default function App() {
  return (
    <CompareProvider>
      <Nav />
      <main className="site-main">
        <Outlet />
      </main>
      <footer className="site-footer">
        <p>Ball data sourced from manufacturer sites. Not affiliated with any manufacturer listed here.</p>
      </footer>
    </CompareProvider>
  );
}
