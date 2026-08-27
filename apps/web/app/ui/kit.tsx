"use client";

import { ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { cx, HEAT_HEX, Point, simBand } from "../data";

/** The primitives every view is built from. Mono is a first-class citizen:
 *  labels, data, IDs, and vectors are all set in JetBrains Mono; only prose
 *  is Inter. That single rule carries most of the console's personality. */

// -------------------------------- brand ---------------------------------

/** The mark: three points and the vector between them. A tiny nearest-neighbour. */
export function Mark({ size = 22 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden>
      <line x1="5" y1="18" x2="17" y2="7" stroke="#7c8cff" strokeWidth="1.5" />
      <line x1="5" y1="18" x2="12" y2="16" stroke="#4fd6c9" strokeWidth="1.5" />
      <circle cx="5" cy="18" r="2.4" fill="#6b74ff" />
      <circle cx="17" cy="7" r="2.4" fill="#c6f45f" />
      <circle cx="12" cy="16" r="1.8" fill="#4fd6c9" />
    </svg>
  );
}

export function Logo() {
  return (
    <div className="flex items-center gap-2">
      <Mark />
      <span className="font-mono text-sm font-semibold tracking-tight text-ink">
        mark<span className="text-accent">vector</span>
      </span>
    </div>
  );
}

// -------------------------------- text ----------------------------------

/** A mono eyebrow label — the console's structural signal. */
export function Label({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <span
      className={cx(
        "font-mono text-2xs uppercase tracking-[0.14em] text-subtle",
        className
      )}
    >
      {children}
    </span>
  );
}

export function Mono({ children, className }: { children: ReactNode; className?: string }) {
  return <span className={cx("font-mono", className)}>{children}</span>;
}

// -------------------------------- chips ---------------------------------

export function Chip({
  children,
  tone = "text-muted border-edgeStrong bg-elevated",
  onClick,
  active,
  title,
}: {
  children: ReactNode;
  tone?: string;
  onClick?: () => void;
  active?: boolean;
  title?: string;
}) {
  const Tag = onClick ? "button" : "span";
  return (
    <Tag
      onClick={onClick}
      title={title}
      className={cx(
        "inline-flex items-center gap-1 rounded-md border px-2 py-0.5 font-mono text-2xs font-medium",
        tone,
        onClick && "transition hover:brightness-125",
        active && "ring-1 ring-accent/60"
      )}
    >
      {children}
    </Tag>
  );
}

export function Button({
  children,
  onClick,
  variant = "ghost",
  type = "button",
  disabled,
  size = "md",
  className,
}: {
  children: ReactNode;
  onClick?: () => void;
  variant?: "primary" | "ghost" | "danger" | "hot";
  type?: "button" | "submit";
  disabled?: boolean;
  size?: "sm" | "md";
  className?: string;
}) {
  const tones = {
    primary: "bg-accent text-canvas hover:bg-accentSoft border-accent font-semibold",
    ghost: "bg-elevated text-ink hover:bg-raised border-edge",
    danger: "bg-transparent text-danger hover:bg-danger/10 border-danger/40",
    hot: "bg-hot/15 text-hot hover:bg-hot/25 border-hot/40",
  };
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className={cx(
        "rounded-lg border font-medium transition disabled:cursor-not-allowed disabled:opacity-40",
        size === "sm" ? "px-2.5 py-1 text-2xs" : "px-3.5 py-2 text-xs",
        tones[variant],
        className
      )}
    >
      {children}
    </button>
  );
}

// ------------------------------ containers -------------------------------

export function Card({
  children,
  className,
  onClick,
  hover,
}: {
  children: ReactNode;
  className?: string;
  onClick?: () => void;
  hover?: boolean;
}) {
  return (
    <div
      onClick={onClick}
      className={cx(
        "rounded-xl border border-edge bg-panel",
        (hover || onClick) &&
          "cursor-pointer transition hover:border-edgeStrong hover:bg-elevated",
        className
      )}
    >
      {children}
    </div>
  );
}

export function Stat({
  value,
  label,
  tone,
}: {
  value: ReactNode;
  label: string;
  tone?: string;
}) {
  return (
    <div>
      <div className={cx("font-mono text-xl font-semibold tabular-nums", tone || "text-ink")}>
        {value}
      </div>
      <Label className="mt-0.5 block">{label}</Label>
    </div>
  );
}

export function Empty({ title, hint, action }: { title: string; hint?: string; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-edge px-6 py-16 text-center">
      <p className="text-sm text-ink">{title}</p>
      {hint && <p className="mt-1.5 max-w-md text-xs leading-relaxed text-subtle">{hint}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function Field({
  label,
  value,
  onChange,
  placeholder,
  mono,
  type = "text",
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  mono?: boolean;
  type?: string;
}) {
  return (
    <label className="block">
      <Label className="mb-1.5 block">{label}</Label>
      <input
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className={cx(
          "w-full rounded-lg border border-edge bg-canvas px-3 py-2 text-sm text-ink",
          "outline-none transition placeholder:text-subtle focus:border-accent/60",
          mono && "font-mono text-xs"
        )}
      />
    </label>
  );
}

// ------------------------------- copy/code -------------------------------

export function Copy({ text, label }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      onClick={() => {
        navigator.clipboard?.writeText(text).then(
          () => {
            setDone(true);
            setTimeout(() => setDone(false), 1400);
          },
          () => {}
        );
      }}
      className={cx(
        "inline-flex items-center gap-1.5 rounded-md border px-2 py-1 font-mono text-2xs transition",
        done
          ? "border-success/40 bg-success/10 text-success"
          : "border-edge bg-elevated text-muted hover:text-ink"
      )}
    >
      {done ? "copied" : label || "copy"}
    </button>
  );
}

export function Code({
  code,
  lang,
  filename,
}: {
  code: string;
  lang?: string;
  filename?: string;
}) {
  return (
    <div className="overflow-hidden rounded-xl border border-edge bg-canvas">
      <div className="flex items-center justify-between border-b border-edge bg-elevated/60 px-3 py-1.5">
        <Label>{filename || lang || "shell"}</Label>
        <Copy text={code} />
      </div>
      <pre className="overflow-x-auto px-4 py-3.5 font-mono text-xs leading-relaxed text-muted">
        <code dangerouslySetInnerHTML={{ __html: highlight(code) }} />
      </pre>
    </div>
  );
}

/** Tiny, dependency-free highlighter: enough to lift keywords, strings and
 *  our own key tokens off the page without pulling in a syntax library.
 *
 *  ONE pass, not a chain of replacements. Chaining them meant each rule ran
 *  over the HTML the previous rules had already emitted — the comment rule
 *  matched the `#7fe0a0` inside a span's own colour attribute and wrapped it,
 *  spilling `#9aa6ff">curl` into the rendered snippet. Matching once and
 *  escaping every span's contents makes that impossible by construction.
 */
const ESC: Record<string, string> = { "&": "&amp;", "<": "&lt;", ">": "&gt;" };
const esc = (s: string) => s.replace(/[&<>]/g, (c) => ESC[c]);

const KEY_TOKEN = /\b((?:mvk|kb)_(?:live|test)_[A-Za-z0-9_-]+)\b/g;
const COLOR = {
  comment: "#5b6673",
  string: "#7fe0a0",
  key: "#ff9d5c",
  keyword: "#9aa6ff",
} as const;

const TOKENS = new RegExp(
  [
    "(?<comment>#[^\\n]*|//[^\\n]*)",
    "(?<string>\"(?:[^\"\\\\\\n]|\\\\.)*\"|'(?:[^'\\\\\\n]|\\\\.)*')",
    "(?<key>\\b(?:mvk|kb)_(?:live|test)_[A-Za-z0-9_-]+\\b)",
    "(?<keyword>\\b(?:import|from|const|let|await|async|def|func|package|return|" +
      "new|export|pip|npm|go|curl|client|print)\\b)",
  ].join("|"),
  "g"
);

function highlight(src: string): string {
  let out = "";
  let last = 0;
  for (const match of src.matchAll(TOKENS)) {
    const at = match.index ?? 0;
    out += esc(src.slice(last, at));
    const groups = match.groups || {};
    const kind = (Object.keys(COLOR) as (keyof typeof COLOR)[]).find((k) => groups[k]);
    // A key inside a quoted string still deserves its own colour — that is the
    // token a reader is scanning for.
    const body =
      kind === "string"
        ? esc(match[0]).replace(
            KEY_TOKEN,
            `<span style="color:${COLOR.key}">$1</span>`
          )
        : esc(match[0]);
    out += kind ? `<span style="color:${COLOR[kind]}">${body}</span>` : body;
    last = at + match[0].length;
  }
  return out + esc(src.slice(last));
}

// -------------------------------- heat ----------------------------------

/** The brand device rendered as a legend — cold (far) to hot (near). */
export function HeatLegend() {
  const labels = ["≥.90", ".80", ".68", ".55", "<.55"];
  return (
    <div className="flex items-center gap-2">
      <Label>near</Label>
      <div className="flex overflow-hidden rounded-md">
        {HEAT_HEX.map((h, i) => (
          <span
            key={i}
            title={`cos ${labels[i]}`}
            style={{ background: h }}
            className="h-2.5 w-6"
          />
        ))}
      </div>
      <Label>far</Label>
    </div>
  );
}

/** A single similarity score shown on the heat scale — a bar plus the number. */
export function ScoreBar({ sim }: { sim: number }) {
  const band = simBand(sim);
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-16 overflow-hidden rounded-full bg-elevated">
        <div
          style={{ width: `${Math.round(sim * 100)}%`, background: HEAT_HEX[band] }}
          className="h-full rounded-full"
        />
      </div>
      <Mono className="text-2xs tabular-nums" >
        <span style={{ color: HEAT_HEX[band] }}>{sim.toFixed(3)}</span>
      </Mono>
    </div>
  );
}

// ------------------------- signature: vector field -----------------------

type FieldPoint = { x: number; y: number; r: number; band: 0 | 1 | 2 | 3 | 4 };

/**
 * The page's signature. A projected embedding space: hundreds of ambient
 * points, a bright query point at the centre, and its k nearest neighbours
 * lit on the heat scale with a line drawn to each. Deterministic so it never
 * jitters between renders; neighbours can be passed in to mirror real hits.
 */
export function VectorField({
  neighbours,
  className,
  height = 300,
}: {
  neighbours?: Point[];
  className?: string;
  height?: number;
}) {
  const W = 640;
  const H = height;
  const { ambient, hits, query } = useMemo(() => {
    let seed = 424242;
    const rnd = () => ((seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff);
    // Coordinates are rounded before they reach an attribute. The layout is
    // already deterministic — seeded PRNG, fixed inputs — but Math.cos and
    // Math.sin may differ in the final ulp between Node and the browser, and
    // React compares the rendered strings: "-22.33664930335874" against
    // "-22.33664930335877" is a hydration mismatch on every point. Two
    // decimals is more precision than an SVG pixel can express anyway.
    const p2 = (n: number) => Math.round(n * 100) / 100;
    const query = { x: p2(W * 0.5), y: p2(H * 0.52) };
    const ambient: FieldPoint[] = [];
    for (let i = 0; i < 220; i++) {
      const a = rnd() * Math.PI * 2;
      const rad = 40 + rnd() * (Math.min(W, H) * 0.62);
      ambient.push({
        x: p2(query.x + Math.cos(a) * rad * (0.9 + rnd() * 0.5)),
        y: p2(query.y + Math.sin(a) * rad * (0.55 + rnd() * 0.4)),
        r: p2(1 + rnd() * 1.4),
        band: 4,
      });
    }
    const source =
      neighbours && neighbours.length
        ? neighbours.slice(0, 5)
        : [0.95, 0.88, 0.81, 0.72, 0.63].map((s) => ({ score: s }) as Point);
    const hits = source.map((p, i) => {
      const a = (i / source.length) * Math.PI * 2 + 0.5;
      const rad = 34 + (1 - p.score) * 210;
      return {
        x: p2(query.x + Math.cos(a) * rad),
        y: p2(query.y + Math.sin(a) * rad * 0.72),
        r: p2(4.5 - i * 0.4),
        band: simBand(p.score),
        score: p.score,
      };
    });
    return { ambient, hits, query };
  }, [neighbours, H]);

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      className={cx("h-auto w-full", className)}
      preserveAspectRatio="xMidYMid slice"
      role="img"
      aria-label="Projected embedding space with the query point and its nearest neighbours"
    >
      {ambient.map((p, i) => (
        <circle key={i} cx={p.x} cy={p.y} r={p.r} fill="#2a323d" opacity={0.5} />
      ))}
      {hits.map((h, i) => (
        <line
          key={`l${i}`}
          x1={query.x}
          y1={query.y}
          x2={h.x}
          y2={h.y}
          stroke={HEAT_HEX[h.band]}
          strokeWidth={1}
          opacity={0.4}
        />
      ))}
      {hits.map((h, i) => (
        <g key={`h${i}`}>
          <circle cx={h.x} cy={h.y} r={h.r + 5} fill={HEAT_HEX[h.band]} opacity={0.12} />
          <circle cx={h.x} cy={h.y} r={h.r} fill={HEAT_HEX[h.band]} />
        </g>
      ))}
      <circle cx={query.x} cy={query.y} r={13} fill="#7c8cff" opacity={0.16} className="animate-ping2" />
      <circle cx={query.x} cy={query.y} r={5.5} fill="#e7ecf2" />
      <circle cx={query.x} cy={query.y} r={5.5} fill="none" stroke="#7c8cff" strokeWidth="1.5" />
    </svg>
  );
}

// -------------------------------- toast ---------------------------------

export function useToast() {
  const [msg, setMsg] = useState<string | null>(null);
  // React 19 requires useRef to be given an initial value — an ref with no
  // argument is no longer implicitly undefined. Same behaviour, said out loud.
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const toast = (m: string) => {
    setMsg(m);
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setMsg(null), 2600);
  };
  const node = msg ? (
    <div className="pointer-events-none fixed bottom-6 left-1/2 z-50 -translate-x-1/2 card-in">
      <div className="rounded-lg border border-edgeStrong bg-raised px-4 py-2 font-mono text-xs text-ink shadow-xl shadow-black/40">
        {msg}
      </div>
    </div>
  ) : null;
  return { toast, node };
}

export function useTick(ms = 4000) {
  const [, set] = useState(0);
  useEffect(() => {
    const t = setInterval(() => set((n) => n + 1), ms);
    return () => clearInterval(t);
  }, [ms]);
}
