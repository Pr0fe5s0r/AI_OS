"use client";

import { ReactNode } from "react";
import { cx } from "../lib";

/** The small set of primitives every view is built from. */

export function Chip({
  children,
  tone = "text-muted border-edgeStrong bg-elevated",
  onClick,
  title,
  active,
}: {
  children: ReactNode;
  tone?: string;
  onClick?: () => void;
  title?: string;
  active?: boolean;
}) {
  const Tag = onClick ? "button" : "span";
  return (
    <Tag
      onClick={onClick}
      title={title}
      className={cx(
        "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-2xs font-medium",
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
  className,
}: {
  children: ReactNode;
  onClick?: () => void;
  variant?: "primary" | "ghost" | "danger";
  type?: "button" | "submit";
  disabled?: boolean;
  className?: string;
}) {
  const tones = {
    primary: "bg-accent text-white hover:bg-accentSoft border-accent",
    ghost: "bg-elevated text-ink hover:bg-raised border-edge",
    danger: "bg-transparent text-danger hover:bg-danger/10 border-danger/40",
  };
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className={cx(
        "rounded-lg border px-3 py-1.5 text-xs font-medium transition",
        "disabled:cursor-not-allowed disabled:opacity-40",
        tones[variant],
        className
      )}
    >
      {children}
    </button>
  );
}

export function Empty({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-edge px-6 py-16 text-center">
      <p className="text-sm text-ink">{title}</p>
      {hint && <p className="mt-1.5 max-w-md text-xs leading-relaxed text-subtle">{hint}</p>}
    </div>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 py-8 text-xs text-subtle">
      <span className="h-3 w-3 animate-spin rounded-full border-2 border-edgeStrong border-t-accent" />
      {label || "Loading…"}
    </div>
  );
}

export function Field({
  label,
  value,
  onChange,
  placeholder,
  mono,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  mono?: boolean;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-2xs uppercase tracking-wide text-subtle">{label}</span>
      <input
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

/** A statistic with its label — used sparingly, in the header only. */
export function Stat({ value, label }: { value: ReactNode; label: string }) {
  return (
    <div>
      <div className="text-lg font-semibold tabular-nums text-ink">{value}</div>
      <div className="text-2xs uppercase tracking-wide text-subtle">{label}</div>
    </div>
  );
}

/** Minimal Markdown rendering: headings, rules, lists, paragraphs.
 *  Deliberately not a full parser — the body is our own normalised output. */
export function Markdown({ body }: { body: string }) {
  const blocks = body.split(/\n{2,}/);
  return (
    <div className="space-y-3">
      {blocks.map((block, i) => {
        const text = block.trim();
        if (!text) return null;
        if (/^<!--\s*page/.test(text)) {
          return (
            <div key={i} className="pt-1 text-2xs uppercase tracking-wide text-subtle">
              {text.replace(/<!--\s*|\s*-->/g, "")}
            </div>
          );
        }
        if (/^-{3,}$/.test(text)) {
          return <hr key={i} className="border-edge" />;
        }
        const heading = text.match(/^(#{1,3})\s+(.*)$/);
        if (heading) {
          const size = heading[1].length === 1 ? "text-base" : "text-sm";
          return (
            <h3 key={i} className={cx("font-semibold text-ink", size)}>
              {heading[2]}
            </h3>
          );
        }
        if (/^[-*]\s+/m.test(text)) {
          return (
            <ul key={i} className="list-disc space-y-1 pl-5 text-sm leading-relaxed text-muted">
              {text.split("\n").map((line, j) => (
                <li key={j}>{line.replace(/^[-*]\s+/, "")}</li>
              ))}
            </ul>
          );
        }
        return (
          <p key={i} className="whitespace-pre-wrap text-sm leading-relaxed text-muted">
            {text}
          </p>
        );
      })}
    </div>
  );
}
