import { NavLink } from "react-router-dom";
import { useCompare } from "../context/CompareContext";

// Persistent top nav across every route -- react-router's NavLink swaps
// the page content without a full reload, matching Al's ask directly:
// "a single page like site for quick navigation and not a ton of full
// page reloads to navigate the content".
export default function Nav() {
  const { ids } = useCompare();

  return (
    <header className="site-header">
      <div className="site-header-inner">
        <NavLink to="/" className="brand" end>
          Bowler Depot Ball Finder
        </NavLink>
        <nav className="main-nav">
          <NavLink to="/" end className={({ isActive }) => (isActive ? "active" : "")}>
            Browse
          </NavLink>
          <NavLink to="/plotter" className={({ isActive }) => (isActive ? "active" : "")}>
            Motion Plotter
          </NavLink>
          <NavLink to="/compare" className={({ isActive }) => (isActive ? "active" : "")}>
            Compare{ids.length > 0 ? ` (${ids.length})` : ""}
          </NavLink>
        </nav>
      </div>
    </header>
  );
}
