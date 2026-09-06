import { Component, type ErrorInfo, type ReactNode } from "react";
import Button from "./Button";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

// Real incident that prompted this: VideoCandidatesPage's first
// browser run threw a TypeError from a wrong type assumption
// (match_confidence typed as a number, actually a text enum -- see
// api/types.ts) and blanked the ENTIRE app, not just that page --
// nothing in the tree caught the render error. React class components
// are still the only way to implement getDerivedStateFromError; there
// is no hooks equivalent. Wrapping <Outlet/> in Layout.tsx means a bug
// in one page can't take down sign-in/navigation for every other page.
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // eslint-disable-next-line no-console
    console.error("Unhandled error in page render:", error, info.componentStack);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="flex flex-col items-start gap-3 rounded-lg border border-danger bg-danger-light p-6">
          <h2 className="text-sm font-semibold text-red-900">Something went wrong on this page</h2>
          <p className="max-w-xl text-sm text-red-800">{this.state.error.message}</p>
          <p className="text-xs text-red-700">
            Check the browser console for the full stack trace. Other pages should still work -- try navigating away and
            back.
          </p>
          <Button variant="secondary" size="sm" onClick={() => this.setState({ error: null })}>
            Try again
          </Button>
        </div>
      );
    }
    return this.props.children;
  }
}
