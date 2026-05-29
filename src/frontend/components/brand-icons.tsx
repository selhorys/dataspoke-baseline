/**
 * Single-color brand glyphs for the header's infra links, drawn to mirror the
 * shape of each product's logo. They use `currentColor` and accept a
 * `className` so they render identically to the lucide-react icons beside them
 * (theme-aware, sizable via `h-4 w-4`).
 */
type IconProps = { className?: string };

/** DataHub — broken orbital ring with satellite nodes and an inner arc. */
export function DataHubIcon({ className }: IconProps) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      className={className}
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="7.5" strokeDasharray="10.4 5.3" />
      <path d="M12 8.5 A3.5 3.5 0 0 1 12 15.5" />
      <circle cx="11" cy="19.5" r="1.6" fill="currentColor" stroke="none" />
      <circle cx="6.3" cy="7.3" r="1.6" fill="currentColor" stroke="none" />
      <circle cx="18.5" cy="9.2" r="1.6" fill="currentColor" stroke="none" />
    </svg>
  );
}

/** Airflow — four swept pinwheel blades. */
export function AirflowIcon({ className }: IconProps) {
  const blade = "M12 12 C11 6 14 2 19 3 C18 8 16 11 12 12 Z";
  return (
    <svg
      viewBox="0 0 24 24"
      fill="currentColor"
      className={className}
      aria-hidden="true"
    >
      <path d={blade} />
      <path d={blade} transform="rotate(90 12 12)" />
      <path d={blade} transform="rotate(180 12 12)" />
      <path d={blade} transform="rotate(270 12 12)" />
    </svg>
  );
}

/** Langfuse — two interlocking strands. */
export function LangfuseIcon({ className }: IconProps) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.2"
      strokeLinecap="round"
      className={className}
      aria-hidden="true"
    >
      <path d="M3 7.5 C8 7.5 8 16.5 12 16.5 C16 16.5 16 7.5 21 7.5" />
      <path d="M3 16.5 C8 16.5 8 7.5 12 7.5 C16 7.5 16 16.5 21 16.5" />
    </svg>
  );
}

/** ReDoc — API reference page with a folded corner. */
export function RedocIcon({ className }: IconProps) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      <path d="M18 3.5 H8.5 C4 3.5 2 7 2 11.2 C2 14.8 3.6 17.8 7 19.4 C11.6 21.4 16.6 20 19.5 15.6" />
      <path d="M11 8 H18" />
      <path d="M11 11 H17.5" />
      <path d="M11 14 H16" />
    </svg>
  );
}
