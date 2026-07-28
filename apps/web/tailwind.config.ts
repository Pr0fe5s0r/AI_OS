import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // A calm, near-black canvas. A knowledge base is something people read
        // in for long stretches, so contrast is deliberate and low-noise: one
        // accent, and colour reserved for meaning (confidence, lifecycle).
        canvas: "#08090b",
        panel: "#0d0f12",
        elevated: "#13161a",
        raised: "#191d22",
        edge: "#20252b",
        edgeStrong: "#2c333b",
        ink: "#e9edf2",
        muted: "#8d97a3",
        subtle: "#626d7a",
        accent: "#6366f1",
        accentSoft: "#818cf8",
        success: "#34d399",
        warn: "#fbbf24",
        danger: "#f87171",
        info: "#60a5fa",
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "sans-serif"],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "Cascadia Code", "Consolas", "monospace"],
      },
      fontSize: {
        "2xs": ["0.6875rem", { lineHeight: "1rem" }],
      },
      keyframes: {
        rise: {
          "0%": { opacity: "0", transform: "translateY(4px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        slide: {
          "0%": { opacity: "0", transform: "translateX(16px)" },
          "100%": { opacity: "1", transform: "translateX(0)" },
        },
      },
      animation: {
        rise: "rise .18s ease-out both",
        slide: "slide .2s ease-out both",
      },
    },
  },
  plugins: [],
};

export default config;
