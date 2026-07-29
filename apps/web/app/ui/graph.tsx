"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { HEAT_HEX, cx, simBand } from "../data";

/**
 * The collection as a neighbour graph.
 *
 * A node is a PASSAGE, not a document. One node per file drew a collection of
 * a dozen documents as a dozen unconnected dots — nothing to look at, and
 * nothing true either, because a document is not one idea. Passages are what
 * was embedded, so passages are what can honestly be drawn.
 *
 * Two kinds of edge, kept visually distinct because they mean different
 * things: a similarity edge is an OBSERVATION about the vectors, while a
 * same-document edge is a FACT the store recorded. Drawing them alike would
 * assert something untrue about the data.
 *
 * The layout is a small force simulation — repulsion between every pair,
 * springs along the edges, a pull toward the centre — run to a fixed number of
 * steps so the same data always settles into the same picture.
 *
 * No layout library: the whole simulation is thirty lines, and a dependency
 * that ships a renderer, a physics engine and its own event system to draw a
 * few hundred circles is a poor trade.
 */

export type GraphNode = {
  id: string;
  /** The passage's heading path — what this point actually is. */
  title: string;
  source: string;
  degree: number;
  category: string | null;
  categoryName: string | null;
  /** Which document the passage belongs to. Colour keys off this, so the
   *  passages of one file read as a group and a passage sitting far from its
   *  own siblings is visible — usually meaning the file covers two subjects. */
  itemId?: string;
  ordinal?: number;
  document?: string;
  /** "summary" means the text was written by a model from other passages, not
   *  lifted from a document. Drawn differently because it is a different kind
   *  of claim. */
  nodeType?: string;
  /** 1 raw · 2 connected · 3 working-memory hub · 4 long-term. */
  stage?: number;
  /** Position in embedding space, 0..1, from the projection. Unlike the force
   *  layout these coordinates carry meaning, so distance can be read. */
  px?: number | null;
  py?: number | null;
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

/** A stable colour per group, so a document's passages read as one shape. */
const CATEGORY_HUES = ["#7c8cff", "#4fd6c9", "#c6f45f", "#ff9d5c", "#f5c451", "#ff7a7a", "#9aa6ff"];

/** Passages are grouped by their document; a node with neither falls back to
 *  its category, which is what a document-level graph used to key on. */
const groupKey = (n: GraphNode): string | null => n.itemId ?? n.category;

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
    itemId: p.itemId,
    ordinal: p.ordinal,
    document: p.document,
    nodeType: p.nodeType,
    stage: p.stage,
    // Rounded, because these become SVG attributes and a server/client float
    // disagreement in the last digit is a hydration mismatch per node.
    x: round(p.x * scale + offsetX),
    y: round(p.y * scale + offsetY),
    r: round(5 + (p.degree / maxDegree) * 7),
  }));
}

/** The projection laid out in the same coordinate space as the force layout,
 *  so the two views are interchangeable to everything downstream. */
function placeByProjection(nodes: GraphNode[]): Placed[] {
  const pad = 46;
  const maxDegree = Math.max(1, ...nodes.map((n) => n.degree));
  const round = (n: number) => Math.round(n * 100) / 100;
  return nodes
    .filter((n) => n.px != null && n.py != null)
    .map((n) => ({
      ...n,
      x: round(pad + (n.px as number) * (W - pad * 2)),
      // SVG y grows downward; the projection's does not, so it is flipped to
      // keep the picture the same way up as the numbers.
      y: round(pad + (1 - (n.py as number)) * (H - pad * 2)),
      r: round(5 + (n.degree / maxDegree) * 7),
    }));
}

export function CollectionGraph({
  nodes,
  edges,
  mode = "graph",
  onOpen,
}: {
  nodes: GraphNode[];
  edges: GraphEdge[];
  /** `graph` runs the force layout; `map` uses the projection's real
   *  coordinates. The distinction matters: only one of them lets you read
   *  distance. */
  mode?: "graph" | "map";
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
  }, [nodes, edges, mode]);

  const categoryIndex = useMemo(() => {
    const index = new Map<string, number>();
    nodes.forEach((n) => {
      const key = groupKey(n);
      if (key && !index.has(key)) index.set(key, index.size);
    });
    return index;
  }, [nodes]);

  const placed = useMemo(
    () => (!ready ? [] : mode === "map" ? placeByProjection(nodes) : layout(nodes, edges)),
    [nodes, edges, ready, mode]
  );
  const byId = useMemo(() => new Map(placed.map((p) => [p.id, p])), [placed]);

  const legend = useMemo(() => {
    const seen = new Map<string, string>();
    nodes.forEach((n) => {
      const key = groupKey(n);
      const label = n.document || n.categoryName;
      if (key && label && !seen.has(key)) seen.set(key, label);
    });
    // A legend naming forty documents is not a legend. Past a handful the
    // colours still group, but the list stops being readable.
    return [...seen.entries()].slice(0, 8);
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
          Nothing embedded yet — add documents and their passages appear here.
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
              // Three cases, drawn differently because they claim different
              // things: consecutive passages of one file (a fact the store
              // recorded), a link the store declared between documents, and a
              // similarity the vectors imply (an observation).
              const sibling = e.kind === "same_document";
              const declared = !sibling && e.kind !== "similarity";
              const dim = hover && !(connected.has(e.src) && connected.has(e.dst));
              return (
                <line
                  key={i}
                  x1={a.x}
                  y1={a.y}
                  x2={b.x}
                  y2={b.y}
                  stroke={sibling ? "#3a4250" : declared ? "#ff9d5c" : HEAT_HEX[simBand(e.similarity)]}
                  strokeWidth={sibling ? 1 : declared ? 1.6 : 0.5 + e.similarity * 1.4}
                  strokeDasharray={declared ? "4 3" : sibling ? "2 3" : undefined}
                  opacity={
                    dim
                      ? 0.06
                      : sibling
                        ? 0.5
                        : declared
                          ? 0.9
                          : (mode === "map" ? 0.1 : 0.22) + e.similarity * (mode === "map" ? 0.18 : 0.4)
                  }
                />
              );
            })}
          </g>

          <g>
            {placed.map((p) => {
              const dim = hover ? !connected.has(p.id) : false;
              const colour = categoryColour(groupKey(p), categoryIndex);
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
                  {/* A summary is text a model wrote. It gets a ring so it is
                      never mistaken for a passage from a document. */}
                  {p.nodeType === "summary" && (
                    <circle
                      cx={p.x}
                      cy={p.y}
                      r={p.r + 3.5}
                      fill="none"
                      stroke="#f5c451"
                      strokeWidth="1.4"
                      opacity={0.9}
                    />
                  )}
                </g>
              );
            })}
          </g>
        </svg>
      )}

      {/* what the eye is looking at */}
      {focused && (
        <div className="pointer-events-none absolute left-3 top-3 max-w-[22rem] rounded-lg border border-edgeStrong bg-raised/95 px-3 py-2 backdrop-blur">
          <div className="truncate text-xs text-ink">
            {focused.title || "(opening passage)"}
          </div>
          {focused.document && (
            <div className="mt-0.5 truncate font-mono text-2xs text-muted">
              {focused.document}
            </div>
          )}
          <div className="mt-0.5 font-mono text-2xs text-subtle">
            {focused.ordinal != null ? `passage ${focused.ordinal + 1} · ` : ""}
            {focused.degree} neighbour{focused.degree === 1 ? "" : "s"}
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
                stroke="#3a4250"
                strokeWidth="1"
                strokeDasharray="2 3"
              />
            </svg>
            same document
          </span>
          {nodes.some((n) => n.nodeType === "summary") && (
            <span className="flex items-center gap-1.5 font-mono text-2xs text-subtle">
              <svg width="12" height="12">
                <circle cx="6" cy="6" r="3" fill="#5b6673" />
                <circle cx="6" cy="6" r="5" fill="none" stroke="#f5c451" strokeWidth="1.2" />
              </svg>
              written by the store
            </span>
          )}
        </span>
      </div>
    </div>
  );
}
