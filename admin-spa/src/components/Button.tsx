import type { ButtonHTMLAttributes } from "react";

type Variant = "primary" | "secondary" | "danger" | "ghost";
type Size = "sm" | "md";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
}

const VARIANT_CLASSES: Record<Variant, string> = {
  // primary-hover (not primary-dark) -- primary-dark is a deliberately
  // light/bright indigo now (see tailwind.config.js), used as text on top
  // of the dark primary-light chip background elsewhere; a button hover
  // needs a darker shade of the solid fill instead, so it gets its own key.
  primary: "bg-primary text-white hover:bg-primary-hover disabled:bg-primary/50",
  secondary: "bg-ink-100 text-ink-700 border border-ink-300 hover:bg-ink-50 disabled:text-ink-400",
  danger: "bg-danger text-white hover:bg-danger-dark disabled:bg-danger/50",
  // ink-200, not ink-100 -- ghost buttons usually sit on an ink-100 card
  // or table row, so a same-tone hover would be invisible.
  ghost: "bg-transparent text-ink-600 hover:bg-ink-200 disabled:text-ink-300",
};

const SIZE_CLASSES: Record<Size, string> = {
  sm: "px-2.5 py-1 text-xs",
  md: "px-3.5 py-2 text-sm",
};

export default function Button({ variant = "secondary", size = "md", className = "", ...rest }: ButtonProps) {
  return (
    <button
      className={`inline-flex items-center gap-1.5 rounded-md font-medium transition-colors disabled:cursor-not-allowed ${VARIANT_CLASSES[variant]} ${SIZE_CLASSES[size]} ${className}`}
      {...rest}
    />
  );
}
