import { useEffect } from "react";
import { useLocation } from "react-router-dom";

// Al: "when selecting an article it doesn't start at the top. often you
// will have to scroll back to the top before you can read it." React
// Router's client-side navigation (pushState under the hood) never
// resets scroll position the way a real page load does -- the browser
// just keeps wherever you'd scrolled to on the index page (e.g. deep
// into the article grid) when ArticleDetailPage mounts in its place.
// This is the standard React Router fix: a location-aware component
// that force-scrolls to top on every route change. Rendered once in
// App.tsx, inside <BrowserRouter> but above <Outlet> so it's active
// for every route swap (index <-> article, and article <-> article).
export default function ScrollToTop() {
  const { pathname } = useLocation();

  useEffect(() => {
    window.scrollTo(0, 0);
  }, [pathname]);

  return null;
}
