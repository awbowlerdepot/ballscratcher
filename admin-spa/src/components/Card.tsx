import type { ReactNode } from "react";

interface CardProps {
  title?: string;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}

export default function Card({ title, actions, children, className = "" }: CardProps) {
  return (
    <div className={`rounded-lg border border-ink-200 bg-ink-100 shadow-sm ${className}`}>
      {(title || actions) && (
        <div className="flex items-center justify-between border-b border-ink-200 px-4 py-3">
          {title && <h3 className="text-sm font-semibold text-ink-700">{title}</h3>}
          {actions}
        </div>
      )}
      <div className="p-4">{children}</div>
    </div>
  );
}
