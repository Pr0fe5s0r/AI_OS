"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { HEAT_HEX, cx, simBand } from "../data";

/**
 * The collection as a neighbour graph.
 *
 * Nodes are documents; an edge means one document is among another's nearest
 * neighbours in embedding space, coloured by how near. The layout is a small
 * force simulation — repulsion between every pair, springs along the edges,
 * and a pull toward the centre — run to a fixed number of steps so the same
 * data always settles into the same picture.
 *
 * No layout library: the whole simulation is thirty lines, and a dependency
 * that ships a renderer, a physics engine and its own event system to draw two
 * hundred circles is a poor trade.
 */

export type GraphNode = {
  id: string;
  title: string;
  source: string;
  degree: number;
  category: string | null;
  categoryName: string | null;
};

export type GraphEdge = {
  src: string;
  dst: string;
  similarity: number;
  kind: string;
};

type Placed = GraphNode & { x: number; y: number; r: number };

const W = 900;
const H = 520;

/** A stable colour per category, so clusters read as groups. */
const CATEGORY_HUES = ["#7c8cff", "#4fd6c9", "#c6f45f", "#ff9d5c", "#f5c451", "#ff7a7a", "#9aa6ff"];

function categoryColour(category: string | null, index: Map<string, number>): string {
  if (!category) return "#5b6673";
  return CATEGORY_HUES[(index.get(category) ?? 0) % CATEGORY_HUES.length];
}

function layout(nodes: GraphNode[], edges: GraphEdge[]): Placed[] {
  const n = nodes.length;
  if (n === 0) return [];

  // Deterministic start: a phyllotaxis spiral, so the simulation begins from
  // an even spread rather than a random one that would settle differently on
  // every render.
  const golden = Math.PI * (3 - Math.sqrt(5));
  const pos = nodes.map((node, i) => {
    const radius = Math.sqrt(i / n) * Math.min(W, H) * 0.38;
    const angle = i * golden;
    return {
      ...node,
      x: W / 2 + Math.cos(angle) * radius,
      y: H / 2 + Math.sin(angle) * radius,
      vx: 0,
      vy: 0,
    };
  });

  const byId = new Map(pos.map((p) => [p.id, p]));
  const links = edges
    .map((e) => ({ a: byId.get(e.src), b: byId.get(e.dst), s: e.similarity }))
    .filter((l) => l.a && l.b) as { a: (typeof pos)[0]; b: (typeof pos)[0]; s: number }[];

  const steps = 260;
  for (let step = 0; step < steps; step++) {
    const cooling = 1 - step / steps;

    // Repulsion — every pair pushes apart, which is what stops the graph
    // collapsing into a single blob.
    for (let i = 0; i < pos.length; i++) {
      for (let j = i + 1; j < pos.length; j++) {
        const a = pos[i];
        const b = pos[j];
        let dx = a.x - b.x;
        let dy = a.y - b.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 1) {
          dx = (i % 7) - 3;
          dy = (j % 7) - 3;
          d2 = 25;
        }
        const force = 900 / d2;
        const d = Math.sqrt(d2);
        a.vx += (dx / d) * force;
        a.vy += (dy / d) * force;
        b.vx -= (dx / d) * force;
        b.vy -= (dy / d) * force;
      }
    }

    // Springs — a stronger similarity pulls harder, so near neighbours end up
    // near on screen. That is the whole point of the picture.
    for (const l of links) {
      const dx = l.b.x - l.a.x;
      const dy = l.b.y - l.a.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 1;
      const rest = 70 + (1 - l.s) * 150;
      const force = (d - rest) * 0.012 * l.s;
      l.a.vx += (dx / d) * force;
      l.a.vy += (dy / d) * force;
      l.b.vx -= (dx / d) * force;
      l.b.vy -= (dy / d) * force;
    }

    for (const p of pos) {
      p.vx += (W / 2 - p.x) * 0.004;
      p.vy += (H / 2 - p.y) * 0.004;
      p.x += Math.max(-12, Math.min(12, p.vx * cooling));
      p.y += Math.max(-12, Math.min(12, p.vy * cooling));
      p.vx *= 0.82;
      p.vy *= 0.82;
      p.x = Math.max(24, Math.min(W - 24, p.x));
      p.y = Math.max(24, Math.min(H - 24, p.y));
    }
  }

  // Fit what settled to the space available. The simulation's absolute scale
  // depends on how many points there are — a dozen documents settle into a
  // knot in the middle of a canvas sized for two hundred — so the result is
  // normalised to its own bounding box rather than tuned per collection size.
  const pad = 46;
  const xs = pos.map((p) => p.x);
  const ys = pos.map((p) => p.y);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const spanX = Math.max(1, maxX - minX);
  const spanY = Math.max(1, maxY - minY);
  // One scale for both axes, so the shape is not distorted.
  const scale = Math.min((W - pad * 2) / spanX, (H - pad * 2) / spanY);
  const offsetX = (W - spanX * scale) / 2 - minX * scale;
  const offsetY = (H - spanY * scale) / 2 - minY * scale;

  const maxDegree = Math.max(1, ...pos.map((p) => p.degree));
  const round = (n: number) => Math.round(n * 100) / 100;

  return pos.map((p) => ({
    id: p.id,
    title: p.title,
    source: p.source,
    degree: p.degree,
    category: p.category,
    categoryName: p.categoryName,
    // Rounded, because these become SVG attributes and a server/client float
    // disagreement in the last digit is a hydration mismatch per node.
    x: round(p.x * scale + offsetX),
    y: round(p.y * scale + offsetY),
    r: round(5 + (p.degree / maxDegree) * 7),
  }));
}

export function CollectionGraph({
  nodes,
  edges,
  onOpen,
}: {
  nodes: GraphNode[];
  edges: GraphEdge[];
  onOpen?: (id: string) => void;
}) {
  const [hover, setHover] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const svgRef = useRef<SVGSVGElement>(null);

  // The simulation is heavy enough to be worth keeping off the first paint.
  useEffect(() => {
    setReady(false);
    const id = requestAnimationFrame(() => setReady(true));
    return () => cancelAnimationFrame(id);
  }, [nodes, edges]);

  const categoryIndex = useMemo(() => {
    const index = new Map<string, number>();
    nodes.forEach((n) => {
      if (n.category && !index.has(n.category)) index.set(n.category, index.size);
    });
    return index;
  }, [nodes]);

  const placed = useMemo(() => (ready ? layout(nodes, edges) : []), [nodes, edges, ready]);
  const byId = useMemo(() => new Map(placed.map((p) => [p.id, p])), [placed]);

  const legend = useMemo(() => {
    const seen = new Map<string, string>();
    nodes.forEach((n) => {
      if (n.category && n.categoryName && !seen.has(n.category)) {
        seen.set(n.category, n.categoryName);
      }
    });
    return [...seen.entries()];
  }, [nodes]);

  const focused = hover ? byId.get(hover) : null;
  const connected = useMemo(() => {
    if (!hover) return new Set<string>();
    const set = new Set<string>([hover]);
    edges.forEach((e) => {
      if (e.src === hover) set.add(e.dst);
      if (e.dst === hover) set.add(e.src);
    });
    return set;
  }, [hover, edges]);

  if (!nodes.length) {
    return (
      <div className="flex h-64 items-center justify-center rounded-xl border border-dashed border-edge">
        <p className="text-xs text-subtle">
          Nothing embedded yet — add documents and the shape appears here.
        </p>
      </div>
    );
  }

  return (
    <div className="relative overflow-hidden rounded-xl border border-edge bg-canvas">
      {!ready && (
        <div className="flex h-[320px] items-center justify-center font-mono text-2xs text-subtle">
          settling the layout…
        </div>
      )}

      {ready && (
        <svg
          ref={svgRef}
          viewBox={`0 0 ${W} ${H}`}
          className="block h-[420px] w-full"
          onMouseLeave={() => setHover(null)}
        >
          {/* edges first, so nodes sit on top */}
          <g>
            {edges.map((e, i) => {
              const a = byId.get(e.src);
              const b = byId.get(e.dst);
              if (!a || !b) return null;
              const declared = e.kind !== "similarity";
              const dim = hover && !(connected.has(e.src) && connected.has(e.dst));
              return (
                <line
                  key={i}
                  x1={a.x}
                  y1={a.y}
                  x2={b.x}
                  y2={b.y}
                  stroke={declared ? "#ff9d5c" : HEAT_HEX[simBand(e.similarity)]}
                  strokeWidth={declared ? 1.6 : 0.5 + e.similarity * 1.4}
                  strokeDasharray={declared ? "4 3" : undefined}
                  opacity={dim ? 0.06 : declared ? 0.9 : 0.22 + e.similarity * 0.4}
                />
              );
            })}
          </g>

          <g>
            {placed.map((p) => {
              const dim = hover ? !connected.has(p.id) : false;
              const colour = categoryColour(p.category, categoryIndex);
              return (
                <g
                  key={p.id}
                  onMouseEnter={() => setHover(p.id)}
                  onClick={() => onOpen?.(p.id)}
                  className={cx(onOpen && "cursor-pointer")}
                  opacity={dim ? 0.18 : 1}
                >
                  {p.id === hover && (
                    <circle cx={p.x} cy={p.y} r={p.r + 6} fill={colour} opacity={0.18} />
                  )}
                  <circle
                    cx={p.x}
                    cy={p.y}
                    r={p.r}
                    fill={colour}
                    stroke="#070809"
                    strokeWidth="1.2"
                  />
                </g>
              );
            })}
          </g>
        </svg>
      )}

      {/* what the eye is looking at */}
      {focused && (
        <div className="pointer-events-none absolute left-3 top-3 max-w-[22rem] rounded-lg border border-edgeStrong bg-raised/95 px-3 py-2 backdrop-blur">
          <div className="truncate text-xs text-ink">{focused.title}</div>
          <div className="mt-0.5 font-mono text-2xs text-subtle">
            {focused.source} · {focused.degree} neighbour{focused.degree === 1 ? "" : "s"}
            {focused.categoryName ? ` · ${focused.categoryName}` : ""}
          </div>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-edge px-3 py-2">
        {legend.map(([id, name]) => (
          <span key={id} className="flex items-center gap-1.5 font-mono text-2xs text-subtle">
            <span
              className="h-2 w-2 rounded-full"
              style={{ background: categoryColour(id, categoryIndex) }}
            />
            {name}
          </span>
        ))}
        <span className="ml-auto flex items-center gap-3">
          <span className="flex items-center gap-1.5 font-mono text-2xs text-subtle">
            <svg width="18" height="6">
              <line x1="0" y1="3" x2="18" y2="3" stroke={HEAT_HEX[0]} strokeWidth="2" />
            </svg>
            near
          </span>
          <span className="flex items-center gap-1.5 font-mono text-2xs text-subtle">
            <svg width="18" height="6">
              <line
                x1="0"
                y1="3"
                x2="18"
                y2="3"
                stroke="#ff9d5c"
                strokeWidth="1.6"
                strokeDasharray="4 3"
              />
            </svg>
            recorded link
          </span>
        </span>
      </div>
    </div>
  );
}
