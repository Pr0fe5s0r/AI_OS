import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "MarkVector — Vector database control plane",
  description:
    "Spin up a cluster, upsert embeddings, query them from a chatbot, and read every request in the trace console. One control plane for your vectors.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="" />
        <link
          href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap"
          rel="stylesheet"
        />
      </head>
      <body className="font-sans antialiased">{children}</body>
    </html>
  );
}
