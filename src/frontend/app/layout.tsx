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

/**
 * Serialise a runtime config object into a safe inline script string.
 * Escapes "<" to prevent "</script>" injection from operator-controlled values.
 */
function buildRuntimeConfigScript(config: { apiBaseUrl: string; datahubUrl: string }): string {
  const safe = JSON.stringify(config).replace(/</g, "\\u003c");
  return `window.__DATASPOKE_RUNTIME_CONFIG__ = ${safe};`;
}

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  // Read non-public env vars at request time (server-only; never inlined by Next.js).
  const apiBaseUrl = process.env.DATASPOKE_API_BASE_URL ?? "";
  const datahubUrl = process.env.DATASPOKE_DATAHUB_URL ?? "";

  const runtimeScript = buildRuntimeConfigScript({ apiBaseUrl, datahubUrl });

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
