import { redirect } from "next/navigation";

/**
 * The per-feature validation detail surface moved to the unified /data/[urn]
 * hub. This route redirects, preserving deep links.
 *
 * Next.js URL-decodes the [urn] segment on server render; re-encode once so the
 * target receives a single-encoded URN (matching the link sites).
 */
export default async function ValidationDatasetRedirect({
  params,
}: {
  params: Promise<{ urn: string }>;
}) {
  const { urn } = await params;
  const datasetUrn = urn.startsWith("urn:") ? urn : decodeURIComponent(urn);
  redirect(`/data/${encodeURIComponent(datasetUrn)}`);
}
