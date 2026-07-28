import type { Config } from "tailwindcss";

/**
 * MarkVector control plane — dark, sharpened.
 *
 * The one artifact a vector database has is the embedding: an array of floats
 * and a point in space judged by DISTANCE. So the palette does two jobs.
 *
 *   1. Chrome stays quiet: a cool near-black canvas, one calm iris accent for
 *      interactive surfaces (nav, buttons, focus). No colour without meaning.
 *   2. Colour that DOES mean something is the cosine-distance heat scale
 *      (heat.*): far/cold iris -> aqua -> near/hot lime. It only ever appears
 *      on things that are literally a similarity or a latency — nearest
 *      neighbours, match scores, trace timings. The scale IS the brand.
 */

const config: Config = {
  content: ["./app/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        canvas: "#070809",
        panel: "#0b0d10",
        elevated: "#101318",
        raised: "#161a20",
        edge: "#1d232b",
        edgeStrong: "#2a323d",
        ink: "#e7ecf2",
        muted: "#8894a3",
        subtle: "#5b6673",
        // Calm interactive accent — chrome only, never used to encode data.
        accent: "#7c8cff",
        accentSoft: "#9aa6ff",
        // Secrets run hot: keys, tokens, revoke.
        hot: "#ff9d5c",
        success: "#5fd08a",
        warn: "#f5c451",
        danger: "#ff7a7a",
        // The cosine-distance heat scale. h0 = nearest match, h4 = far.
        heat: {
          0: "#c6f45f",
          1: "#7fe0a0",
          2: "#4fd6c9",
          3: "#59a6ff",
          4: "#6b74ff",
        },
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "sans-serif"],
        mono: [
          "'JetBrains Mono'",
          "ui-monospace",
          "SFMono-Regular",
          "Menlo",
          "Cascadia Code",
          "Consolas",
          "monospace",
        ],
      },
      fontSize: {
        "2xs": ["0.6875rem", { lineHeight: "1rem" }],
      },
      keyframes: {
        rise: {
          "0%": { opacity: "0", transform: "translateY(6px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        slide: {
          "0%": { opacity: "0", transform: "translateX(16px)" },
          "100%": { opacity: "1", transform: "translateX(0)" },
        },
        ping2: {
          "0%": { transform: "scale(1)", opacity: "0.7" },
          "80%,100%": { transform: "scale(2.4)", opacity: "0" },
        },
        drift: {
          "0%,100%": { transform: "translate(0,0)" },
          "50%": { transform: "translate(var(--dx,2px), var(--dy,-2px))" },
        },
      },
      animation: {
        rise: "rise .2s ease-out both",
        slide: "slide .22s ease-out both",
        ping2: "ping2 1.8s cubic-bezier(0,0,.2,1) infinite",
      },
    },
  },
  plugins: [],
};

export default config;
