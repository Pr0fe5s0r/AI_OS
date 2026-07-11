import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Flat, GitHub-Primer-ish dark canvas with a MarkOS green accent.
        canvas: "#0a0a0a",
        panel: "#0f0f0f",
        elevated: "#161616",
        edge: "#242424",
        edgeStrong: "#333333",
        ink: "#e8e8e8",
        muted: "#8b8b8b",
        subtle: "#6a6a6a",
        accent: "#3fb950",
        success: "#3fb950",
        warn: "#d29922",
        danger: "#f85149",
        info: "#58a6ff",
        // source identity
        gh: "#8b949e",
        slack: "#a855f7",
        zendesk: "#2dd4bf",
      },
      fontFamily: {
        sans: ["ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "Roboto", "sans-serif"],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "Cascadia Code", "Consolas", "monospace"],
      },
    },
  },
  plugins: [],
};

export default config;
