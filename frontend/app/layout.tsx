import type { Metadata } from "next";
import localFont from "next/font/local";
import "./globals.css";

/*
 * Fonts are vendored into `fonts/` and loaded with `next/font/local` rather than
 * `next/font/google`.
 *
 * The Google loader fetches at *build* time, and the Docker build has no DNS - so
 * it silently failed and every weight fell back to a system face, meaning the
 * chosen typography never actually shipped. Self-hosting makes the build
 * reproducible offline, removes a third-party request at runtime, and is the
 * behaviour you want in a container anyway.
 */

// Barlow descends from transit signage - the right vernacular for a departure
// board, and condensed enough to keep long route labels on one line.
const ui = localFont({
  src: [
    { path: "../fonts/barlow-400.woff2", weight: "400", style: "normal" },
    { path: "../fonts/barlow-500.woff2", weight: "500", style: "normal" },
    { path: "../fonts/barlow-600.woff2", weight: "600", style: "normal" },
    { path: "../fonts/barlow-700.woff2", weight: "700", style: "normal" },
  ],
  variable: "--font-ui",
  display: "swap",
  fallback: ["ui-sans-serif", "system-ui", "sans-serif"],
});

// Every time, delay and flight number is tabular data; it must align in columns.
const mono = localFont({
  src: [
    { path: "../fonts/plexmono-400.woff2", weight: "400", style: "normal" },
    { path: "../fonts/plexmono-500.woff2", weight: "500", style: "normal" },
    { path: "../fonts/plexmono-600.woff2", weight: "600", style: "normal" },
  ],
  variable: "--font-mono",
  display: "swap",
  fallback: ["ui-monospace", "monospace"],
});

export const metadata: Metadata = {
  title: "IST Flight Reliability Monitor",
  description:
    "Delay and cancellation analytics for direct flights from Istanbul (IST) to Tehran (IKA) and Mashhad (MHD).",
};

export const dynamic = "force-dynamic";

export default function RootLayout({ children }: { children: React.ReactNode }) {
  const apiBase = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
  return (
    <html lang="en" className={`${ui.variable} ${mono.variable}`}>
      <head>
        <script
          dangerouslySetInnerHTML={{
            __html: `window.__API_BASE__=${JSON.stringify(apiBase)};`,
          }}
        />
      </head>
      <body>{children}</body>
    </html>
  );
}
