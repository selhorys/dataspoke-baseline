/**
 * Brand logos for the header's infra links, served as cropped PNG assets from
 * `public/brand/`. DataHub, Langfuse, and Airflow are full-color and render on
 * either theme; ReDoc is monochrome line art with separate light/dark variants
 * toggled by the `dark` class. Each accepts a `className` for sizing (the header
 * passes `h-[18px] w-auto` to keep every logo's aspect ratio).
 */
type IconProps = { className?: string };

/** DataHub — orbital ring with satellite nodes and an inner arc. */
export function DataHubIcon({ className }: IconProps) {
  // eslint-disable-next-line @next/next/no-img-element
  return <img src="/brand/datahub.png" alt="" aria-hidden="true" className={className} />;
}

/** Langfuse — two interlocking strands. */
export function LangfuseIcon({ className }: IconProps) {
  // eslint-disable-next-line @next/next/no-img-element
  return <img src="/brand/langfuse.png" alt="" aria-hidden="true" className={className} />;
}

/** Airflow — four swept pinwheel blades. */
export function AirflowIcon({ className }: IconProps) {
  // eslint-disable-next-line @next/next/no-img-element
  return <img src="/brand/airflow.png" alt="" aria-hidden="true" className={className} />;
}

/** ReDoc — folded document page; light/dark variants follow the theme. */
export function RedocIcon({ className }: IconProps) {
  return (
    <>
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src="/brand/redoc-light.png"
        alt=""
        aria-hidden="true"
        className={`${className ?? ""} dark:hidden`}
      />
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src="/brand/redoc-dark.png"
        alt=""
        aria-hidden="true"
        className={`${className ?? ""} hidden dark:block`}
      />
    </>
  );
}
