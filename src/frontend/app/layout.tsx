import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";
import { Providers } from "./providers";

const inter = Inter({
  subsets: ["latin"],
  variable: "--font-inter",
});

export const metadata: Metadata = {
  title: "DataSpoke",
  description: "DataSpoke — DataHub sidecar extension",
};

// Render the layout per request so the runtime-config env reads below resolve
// from the live process environment instead of being frozen at build time.
export const dynamic = "force-dynamic";

/**
 * Serialise a runtime config object into a safe inline script string.
 * Escapes "<" to prevent "</script>" injection from operator-controlled values.
 */
function buildRuntimeConfigScript(config: {
  apiBaseUrl: string;
  datahubUrl: string;
  langfuseUrl: string;
  langfuseProjectId: string;
  airflowUrl: string;
}): string {
  const safe = JSON.stringify(config).replace(/</g, "\\u003c");
  return `window.__DATASPOKE_RUNTIME_CONFIG__ = ${safe};`;
}

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  // With force-dynamic the layout renders per request, so these server-only
  // env vars are read at request time and never inlined into the client bundle.
  const apiBaseUrl = process.env.DATASPOKE_API_BASE_URL ?? "";
  const datahubUrl = process.env.DATASPOKE_DATAHUB_URL ?? "";
  const langfuseUrl = process.env.DATASPOKE_LANGFUSE_URL ?? "";
  const langfuseProjectId = process.env.DATASPOKE_LANGFUSE_PROJECT_ID ?? "";
  const airflowUrl = process.env.DATASPOKE_AIRFLOW_URL ?? "";

  const runtimeScript = buildRuntimeConfigScript({
    apiBaseUrl,
    datahubUrl,
    langfuseUrl,
    langfuseProjectId,
    airflowUrl,
  });

  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        {/* Inject runtime config before any client bundle executes. */}
        <script dangerouslySetInnerHTML={{ __html: runtimeScript }} />
      </head>
      <body className={`${inter.variable} font-sans antialiased`}>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
