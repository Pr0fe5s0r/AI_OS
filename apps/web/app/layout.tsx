import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "AI OS - Software Command Deck",
  description: "Operating system for software-company signals, risks, and action queues",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="font-sans antialiased">{children}</body>
    </html>
  );
}
