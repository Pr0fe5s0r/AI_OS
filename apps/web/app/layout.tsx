import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "MarkOS",
  description: "The AI work agent that watches your tools, links what happened, and acts with your approval",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="font-sans antialiased">{children}</body>
    </html>
  );
}
