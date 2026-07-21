"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  Dot, GraphThing, Knowledge, Learning, Measurement, Situation, ThingDetail,
  Understanding, api, firstLine, healthColor, humanize, plural, sourceColor,
  statusColor, term,
} from "../lib";
import { MeasurementCard } from "./learning";

type Props = {
  knowledge: Knowledge | null;
  learning: Learning | null;
  understanding: Understanding | null;
  situations: Situation[];
  onAsk: (text: string) => void;
};

type NodeKind = "source" | "work" | "person" | "norm" | "question";
type Focus = "all" | "work" | "people" | "learning";
type MemoryNode = {
  id: string;
  label: string;
  kind: NodeKind;
  x: number;
  y: number;
  color: string;
  thing?: GraphThing;
  measurement?: Measurement;
  situation?: Situation;
  source?: string;
  isNew?: boolean;
};
type MemoryEdge = { from: string; to: string; kind: string; confidence?: number };

const W = 820;
const H = 500;
const KIND_COLOR: Record<NodeKind, string> = {
  source: "#3fb950",
  work: "#58a6ff",
  person: "#56d4dd",
  norm: "#d29922",
  question: "#bc8cff",
};

function formatHours(value: number) {
  if (value < 1) return `${Math.round(value * 60)} min`;
  if (value < 48) return `${value.toFixed(value < 10 ? 1 : 0)} hr`;
  return `${(value / 24).toFixed(1)} days`;
}

function metricName(metric: string) {
  return humanize(metric.replace(/_hours$/, ""));
}

function isPerson(thing: GraphThing) {
  return /person|user|member|engineer|owner/i.test(thing.thing_type ?? "");
}

/* Positions persist across refetches, keyed by node id. Without this, a
   layout computed purely from `index / count` reassigns EVERY node a new
   angle the moment one new thing arrives — the whole map reshuffles on every
   poll and reads as frozen, since nothing you were just looking at holds
   still. Here, only a node that has never been seen before gets a freshly
   computed spot; everything already on screen keeps its place while its
   content (status, title, event count) updates underneath it. */
function networkData(
  knowledge: Knowledge,
  learning: Learning | null,
  clarifications: Situation[],
  positions: Map<string, { x: number; y: number }>,
  seen: Set<string>,
): { nodes: MemoryNode[]; edges: MemoryEdge[] } {
  const nodes: MemoryNode[] = [];
  const edges: MemoryEdge[] = [];
  const enabledSources = knowledge.sources.filter((source) => source.enabled);
  const things = knowledge.graph.things.slice(0, 24);
  const measurements = learning?.measurements ?? [];
  const rightCount = Math.max(1, measurements.length + clarifications.length);

  const at = (id: string, compute: () => { x: number; y: number }) => {
    let pos = positions.get(id);
    if (!pos) {
      pos = compute();
      positions.set(id, pos);
    }
    return pos;
  };
  // "new since last time this component rendered" — used only to give a
  // just-arrived node a brief entrance, never to move an existing one.
  const isNew = (id: string) => {
    const fresh = !seen.has(id);
    seen.add(id);
    return fresh;
  };

  enabledSources.forEach((source, i) => {
    const id = `source:${source.source}`;
    const pos = at(id, () => ({
      x: 78,
      y: 105 + i * Math.min(125, 310 / Math.max(1, enabledSources.length - 1)),
    }));
    nodes.push({
      id, label: humanize(source.source), kind: "source", source: source.source,
      color: sourceColor(source.source), isNew: isNew(id), ...pos,
    });
  });

  const people = things.filter(isPerson);
  const work = things.filter((thing) => !isPerson(thing));
  work.forEach((thing, i) => {
    const angle = (i / Math.max(work.length, 1)) * Math.PI * 2 - Math.PI / 2;
    const pos = at(thing.id, () => ({
      x: 385 + Math.cos(angle) * Math.min(170, 105 + work.length * 4),
      y: 250 + Math.sin(angle) * Math.min(185, 115 + work.length * 4),
    }));
    nodes.push({
      id: thing.id, label: thing.title ?? thing.id, kind: "work", thing,
      source: thing.source ?? undefined, color: "#58a6ff", isNew: isNew(thing.id), ...pos,
    });
    if (thing.source) edges.push({ from: `source:${thing.source}`, to: thing.id, kind: "observed" });
  });

  people.forEach((thing, i) => {
    const pos = at(thing.id, () => ({ x: 280 + i * 112, y: 58 }));
    nodes.push({
      id: thing.id, label: thing.title ?? thing.id, kind: "person", thing,
      color: "#56d4dd", isNew: isNew(thing.id), ...pos,
    });
  });

  const existing = new Set(nodes.map((node) => node.id));
  const usefulGraphEdges = knowledge.graph.links
    .filter((edge) => existing.has(edge.src) && existing.has(edge.dst))
    .sort((a, b) => (a.type === "SAME_AS" ? 1 : 0) - (b.type === "SAME_AS" ? 1 : 0) || b.confidence - a.confidence)
    .slice(0, 22);
  usefulGraphEdges.forEach((edge) => edges.push({ from: edge.src, to: edge.dst, kind: edge.type, confidence: edge.confidence }));

  measurements.forEach((measurement, i) => {
    const id = `norm:${measurement.metric}`;
    const pos = at(id, () => ({ x: 740, y: 78 + (i / rightCount) * 340 }));
    const node: MemoryNode = {
      id, label: metricName(measurement.metric), kind: "norm", measurement,
      source: measurement.measured_from.source, color: "#d29922", isNew: isNew(id), ...pos,
    };
    nodes.push(node);
    if (measurement.measured_from.source) {
      edges.push({ from: `source:${measurement.measured_from.source}`, to: node.id, kind: "learns" });
    }
  });

  clarifications.slice(0, 4).forEach((situation, i) => {
    const id = `question:${situation.id}`;
    const pos = at(id, () => ({ x: 740, y: 78 + ((measurements.length + i) / rightCount) * 340 }));
    const node: MemoryNode = {
      id, label: situation.title, kind: "question", situation,
      color: "#bc8cff", isNew: isNew(id), ...pos,
    };
    nodes.push(node);
    const source = situation.evidence[0]?.source;
    if (source) edges.push({ from: `source:${source}`, to: node.id, kind: "questions" });
    else if (measurements.length) edges.push({ from: `norm:${measurements[0].metric}`, to: node.id, kind: "questions" });
  });

  return {
    nodes,
    edges: edges
      .filter((edge) => nodes.some((node) => node.id === edge.from))
      .filter((edge) => nodes.some((node) => node.id === edge.to)),
  };
}

function focusIncludes(focus: Focus, node: MemoryNode) {
  if (focus === "all") return true;
  if (focus === "work") return node.kind === "source" || node.kind === "work";
  if (focus === "people") return node.kind === "person" || node.kind === "work";
  return node.kind === "source" || node.kind === "norm" || node.kind === "question";
}

function NeuralMap({ knowledge, learning, clarifications, selected, onSelect }: {
  knowledge: Knowledge;
  learning: Learning | null;
  clarifications: Situation[];
  selected: MemoryNode | null;
  onSelect: (node: MemoryNode) => void;
}) {
  const [focus, setFocus] = useState<Focus>("all");
  const positionsRef = useRef<Map<string, { x: number; y: number }>>(new Map());
  const seenRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    // a different company shares no positions with the last one
    positionsRef.current = new Map();
    seenRef.current = new Set();
  }, [knowledge.company.id]);
  const data = useMemo(
    () => networkData(knowledge, learning, clarifications, positionsRef.current, seenRef.current),
    [knowledge, learning, clarifications],
  );
  const byId = useMemo(() => new Map(data.nodes.map((node) => [node.id, node])), [data.nodes]);
  const related = useMemo(() => {
    if (!selected) return null;
    const ids = new Set([selected.id]);
    data.edges.forEach((edge) => {
      if (edge.from === selected.id) ids.add(edge.to);
      if (edge.to === selected.id) ids.add(edge.from);
    });
    return ids;
  }, [data.edges, selected]);

  return <div className="overflow-hidden border border-edge bg-[#0c0d0e]">
    <div className="flex flex-col gap-3 border-b border-edge px-4 py-3.5 sm:flex-row sm:items-center sm:justify-between">
      <div>
        <div className="flex items-center gap-2 text-[12px] font-medium text-ink"><span className="relative flex h-2 w-2"><span className="absolute h-full w-full animate-ping rounded-full bg-accent opacity-30" /><span className="relative h-2 w-2 rounded-full bg-accent" /></span>Knowledge network</div>
        <div className="mt-1 text-[11.5px] text-subtle">{data.nodes.length} memories connected by {data.edges.length} relationships</div>
      </div>
      <div className="flex gap-1 border border-edge bg-canvas p-1">
        {(["all", "work", "people", "learning"] as Focus[]).map((item) => <button key={item} onClick={() => setFocus(item)}
          className="border-none px-2.5 py-1.5 text-[11.5px] capitalize" style={{ background: focus === item ? "#1b1d1f" : "transparent", color: focus === item ? "#e8e8e8" : "#6a6a6a" }}>{item}</button>)}
      </div>
    </div>

    <div className="relative min-h-[420px] overflow-hidden">
      <div className="pointer-events-none absolute left-4 top-4 z-10 flex flex-wrap gap-x-4 gap-y-2 text-[10.5px] text-subtle">
        {(Object.entries(KIND_COLOR) as [NodeKind, string][]).map(([kind, color]) => <span key={kind} className="flex items-center gap-1.5"><Dot color={color} size={5} />{kind === "norm" ? "learned pattern" : kind}</span>)}
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="block min-h-[420px] w-full" role="img" aria-label="AI business memory network">
        <defs>
          <filter id="node-glow" x="-80%" y="-80%" width="260%" height="260%">
            <feGaussianBlur stdDeviation="4" result="blur" />
            <feMerge><feMergeNode in="blur" /><feMergeNode in="SourceGraphic" /></feMerge>
          </filter>
        </defs>
        {data.edges.map((edge, i) => {
          const from = byId.get(edge.from);
          const to = byId.get(edge.to);
          if (!from || !to) return null;
          const visible = focusIncludes(focus, from) && focusIncludes(focus, to);
          const connected = !related || (related.has(from.id) && related.has(to.id));
          const curve = (from.x + to.x) / 2;
          return <path key={`${edge.from}:${edge.to}:${i}`} d={`M ${from.x} ${from.y} C ${curve} ${from.y}, ${curve} ${to.y}, ${to.x} ${to.y}`}
            fill="none" stroke={edge.kind === "SAME_AS" ? "#34383d" : "#3d4750"} strokeWidth={connected ? 1.15 : 0.55}
            strokeDasharray={edge.kind === "SAME_AS" ? "3 5" : "5 9"} className="memory-edge"
            opacity={visible ? (connected ? 0.82 : 0.18) : 0.05} />;
        })}
        {data.nodes.map((node) => {
          const active = selected?.id === node.id;
          const visible = focusIncludes(focus, node);
          const connected = !related || related.has(node.id);
          const radius = node.kind === "source" ? 13 : node.kind === "person" ? 11 : node.kind === "norm" ? 12 : node.kind === "question" ? 10 : 8 + Math.min(node.thing?.events ?? 0, 6);
          return <g key={node.id} onClick={() => onSelect(node)} className={`cursor-pointer${node.isNew ? " memory-node-new" : ""}`} opacity={visible ? (connected ? 1 : 0.2) : 0.07}>
            {active && <circle cx={node.x} cy={node.y} r={radius + 8} fill="none" stroke={node.color} strokeWidth="1" opacity="0.45" className="memory-pulse" />}
            <circle cx={node.x} cy={node.y} r={radius} fill="#0c0d0e" stroke={node.color} strokeWidth={active ? 2.4 : 1.4} style={{ filter: active ? "url(#node-glow)" : undefined }} />
            <circle cx={node.x} cy={node.y} r={Math.max(3, radius - 6)} fill={node.color} opacity={node.kind === "work" ? 0.8 : 1} />
            <text x={node.x} y={node.y + radius + 14} textAnchor="middle" fontSize="10" fill={active ? "#e8e8e8" : "#858b91"}>{firstLine(node.label, 20)}</text>
          </g>;
        })}
      </svg>
    </div>
  </div>;
}

function Fact({ label, value, color }: { label: string; value: string; color?: string }) {
  return <div className="border-b border-edge py-3 last:border-b-0"><div className="text-[10.5px] uppercase tracking-[0.8px] text-subtle">{label}</div><div className="mt-1.5 text-[12.5px] leading-relaxed" style={{ color: color ?? "#e8e8e8" }}>{value}</div></div>;
}

function MemoryInspector({ node, knowledge, understanding, onAsk, onOpenNorm }: {
  node: MemoryNode | null;
  knowledge: Knowledge;
  understanding: Understanding | null;
  onAsk: (text: string) => void;
  onOpenNorm: (metric: string) => void;
}) {
  const [detail, setDetail] = useState<ThingDetail | null>(null);
  useEffect(() => {
    if (!node?.thing) { setDetail(null); return; }
    let live = true;
    api(`/api/knowledge/things/${encodeURIComponent(node.thing.id)}`).then((value) => { if (live) setDetail(value); }).catch(() => { if (live) setDetail(null); });
    return () => { live = false; };
  }, [node]);

  if (!node) {
    return <div className="flex h-full flex-col p-5">
      <div className="text-[10.5px] uppercase tracking-[1px] text-subtle">Memory inspector</div>
      <div className="mt-3 text-[17px] font-semibold text-ink">What MarkOS currently knows</div>
      <p className="mt-2 text-[12.5px] leading-relaxed text-muted">Select any memory in the network to see its source, meaning, and correction path.</p>
      <div className="mt-5">
        <Fact label="Data sources" value={knowledge.sources.filter((item) => item.enabled).map((item) => humanize(item.source)).join(", ") || "None connected"} />
        <Fact label="Business objects" value={knowledge.thing_types.map((item) => `${humanize(item.name)} (${item.count})`).join(", ") || "Still discovering"} />
        <Fact label="Profile state" value={`Version ${knowledge.company.version}, ${knowledge.company.status}`} color="#3fb950" />
      </div>
    </div>;
  }

  const title = node.label;
  return <div className="flex h-full flex-col p-5">
    <div className="flex items-center gap-2 text-[10.5px] uppercase tracking-[1px]" style={{ color: node.color }}><Dot color={node.color} size={6} />{node.kind === "norm" ? "Learned pattern" : node.kind}</div>
    <div className="mt-3 text-[17px] font-semibold leading-snug text-ink">{title}</div>

    {node.kind === "source" && (() => {
      const source = knowledge.sources.find((item) => item.source === node.source);
      const watched = understanding?.watching.find((item) => item.source === node.source);
      return <div className="mt-4"><Fact label="What arrives here" value={source?.produces.map(humanize).join(", ") || "Activity"} /><Fact label="Records learned from" value={String(watched?.records ?? 0)} /><Fact label="Connection health" value={watched?.health ? humanize(watched.health) : watched?.connected ? "Connected" : "Not connected"} color={healthColor(watched?.health)} /></div>;
    })()}

    {(node.kind === "work" || node.kind === "person") && <div className="mt-4">
      <Fact label="Type" value={humanize(node.thing?.thing_type ?? "Unknown")} />
      <Fact label="Learned from" value={humanize(node.thing?.source ?? detail?.events[0]?.source ?? "Linked records")} />
      {node.thing?.status && <Fact label="Current status" value={humanize(node.thing.status)} color={statusColor(node.thing.status)} />}
      <Fact label="Evidence" value={detail ? `${detail.events.length} source ${detail.events.length === 1 ? "record" : "records"}` : "Loading source history..."} />
      {detail?.events.slice(0, 2).map((event) => <div key={event.event_id} className="border-b border-edge py-3"><div className="text-[11px] capitalize text-subtle">{event.source} · {new Date(event.event_time).toLocaleDateString()}</div><div className="mt-1 text-[12px] leading-relaxed text-muted">{firstLine(event.content ?? "", 110)}</div></div>)}
    </div>}

    {node.kind === "norm" && node.measurement && <div className="mt-4">
      <Fact label="Usually" value={node.measurement.samples ? formatHours(node.measurement.typical) : "Still learning"} color="#d29922" />
      <Fact label="Evidence" value={`${node.measurement.samples} completed ${node.measurement.samples === 1 ? "record" : "records"}`} />
      <Fact label="Confidence" value={node.measurement.maturity === "stable" ? "Reliable pattern" : "Still learning"} />
      <button onClick={() => onOpenNorm(node.measurement!.metric)} className="mt-4 w-full border border-edgeStrong bg-transparent px-3 py-2 text-[12px] text-ink hover:bg-elevated">Open calculation</button>
    </div>}

    {node.kind === "question" && node.situation && <div className="mt-4">
      <p className="m-0 text-[12.5px] leading-relaxed text-muted">{node.situation.summary}</p>
      <div className="mt-4 text-[11px] uppercase tracking-[0.8px] text-subtle">Waiting for your decision</div>
      <div className="mt-2 space-y-2">{node.situation.choices?.slice(0, 3).map((choice) => <div key={choice.id} className="border-l border-edgeStrong pl-3 text-[12px] text-muted">{choice.label}</div>)}</div>
    </div>}

    <button onClick={() => onAsk(`Review what you know about "${title}". Explain the evidence and help me correct anything wrong.`)}
      className="mt-auto border border-edgeStrong bg-transparent px-3 py-2 text-[12px] text-ink hover:bg-elevated">Discuss with Agent</button>
  </div>;
}

function NormModal({ measurement, onClose, onAsk }: {
  measurement: Measurement | null;
  onClose: () => void;
  onAsk: (text: string) => void;
}) {
  useEffect(() => {
    const close = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [onClose]);
  return <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto p-4 sm:p-7" style={{ background: "rgba(0,0,0,0.78)" }} onClick={onClose}>
    <div className="mt-[4vh] w-full max-w-[680px] border border-edgeStrong bg-bg shadow-2xl" onClick={(event) => event.stopPropagation()}>
      <div className="flex items-start justify-between gap-4 border-b border-edge p-5"><div><div className="text-[10.5px] uppercase tracking-[1px] text-warn">Learned pattern</div><div className="mt-1 text-[16px] font-semibold text-ink">{measurement ? metricName(measurement.metric) : "Norm calculation"}</div><div className="mt-1 text-[12px] text-subtle">Every point comes from completed work in a connected app.</div></div><button onClick={onClose} className="h-8 w-8 border border-edge bg-transparent text-muted hover:bg-elevated hover:text-ink" aria-label="Close">x</button></div>
      <div className="p-5">{measurement ? <MeasurementCard m={measurement} onAsk={(text) => { onClose(); onAsk(text); }} /> : <div className="text-[13px] text-muted">Not enough completed work yet.</div>}</div>
    </div>
  </div>;
}

function LayerRow({ color, label, value, detail }: { color: string; label: string; value: string; detail: string }) {
  return <div className="grid gap-2 border-b border-edge py-4 last:border-b-0 sm:grid-cols-[160px_180px_1fr] sm:items-center">
    <div className="flex items-center gap-2 text-[12px] font-medium text-ink"><Dot color={color} size={7} />{label}</div>
    <div className="text-[13px] text-ink">{value}</div>
    <div className="text-[12px] leading-relaxed text-muted">{detail}</div>
  </div>;
}

export default function BusinessProfile({ knowledge, learning, understanding, situations, onAsk }: Props) {
  const [selected, setSelected] = useState<MemoryNode | null>(null);
  const [normOpen, setNormOpen] = useState<string | null>(null);
  if (!knowledge) return <div className="mx-auto max-w-[1120px] px-6 pt-9 text-[13px] text-muted">Loading AI business memory...</div>;

  const companyName = knowledge.company.name || humanize(knowledge.company.id) || "Your business";
  const actor = term(knowledge.terms, "actor", "person");
  const people = knowledge.graph.things.filter(isPerson);
  const connected = understanding?.watching.filter((item) => item.connected && item.enabled) ?? [];
  const measurements = learning?.measurements ?? [];
  const learned = measurements.filter((item) => item.samples > 0);
  const clarifications = situations.filter((item) => item.kind === "clarification" && item.status !== "resolved");
  const activeNorm = measurements.find((item) => item.metric === normOpen) ?? null;
  const workTypes = knowledge.thing_types.filter((item) => item.count > 0);
  const watchCount = (understanding?.checks.universal.length ?? 0) + (understanding?.checks.profile.length ?? 0);

  return <div className="mx-auto max-w-[1120px] px-4 pb-20 pt-6 sm:px-7 sm:pt-8">
    <header className="mb-5 flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
      <div>
        <div className="flex items-center gap-2 text-[10.5px] uppercase tracking-[1.2px] text-info"><span className="h-1.5 w-1.5 bg-info" />AI business memory</div>
        <h1 className="mb-0 mt-2 text-[24px] font-semibold text-ink">{companyName}</h1>
        <p className="mb-0 mt-1 max-w-[660px] text-[13px] leading-relaxed text-muted">Everything MarkOS has connected, learned, and is currently questioning about your business.</p>
      </div>
      <div className="flex items-center gap-3">
        <span className="flex items-center gap-2 border border-edge px-3 py-2 text-[11.5px] text-muted"><Dot color={knowledge.company.status === "confirmed" ? "#3fb950" : "#d29922"} size={6} />Profile v{knowledge.company.version} {knowledge.company.status}</span>
        <button onClick={() => onAsk("Review our complete business memory. Tell me what you know, what is uncertain, and help me correct it.")} className="border border-edgeStrong bg-transparent px-3 py-2 text-[11.5px] text-ink hover:bg-elevated">Review with Agent</button>
      </div>
    </header>

    <div className="mb-4 grid border border-edge bg-panel sm:grid-cols-5">
      <LayerStat label="Sources" value={connected.length} color="#3fb950" />
      <LayerStat label="Work memories" value={knowledge.graph.things.length} color="#58a6ff" />
      <LayerStat label={plural(actor)} value={people.length} color="#56d4dd" />
      <LayerStat label="Patterns learned" value={learned.length} color="#d29922" />
      <LayerStat label="Open questions" value={clarifications.length} color="#bc8cff" />
    </div>

    <section className="grid border border-edge bg-panel lg:grid-cols-[minmax(0,1fr)_300px]">
      <NeuralMap knowledge={knowledge} learning={learning} clarifications={clarifications} selected={selected} onSelect={setSelected} />
      <div className="min-h-[500px] border-t border-edge bg-panel lg:border-l lg:border-t-0">
        <MemoryInspector node={selected} knowledge={knowledge} understanding={understanding} onAsk={onAsk} onOpenNorm={setNormOpen} />
      </div>
    </section>

    <section className="mt-8">
      <div className="mb-3"><h2 className="m-0 text-[15px] font-semibold text-ink">How MarkOS models your business</h2><p className="mb-0 mt-1 text-[12.5px] text-muted">A readable view of the same memory network.</p></div>
      <div className="border-y border-edge px-1">
        <LayerRow color="#3fb950" label="Data sources" value={connected.map((item) => humanize(item.source)).join(", ") || "None connected"} detail={`${understanding?.records.total ?? 0} records have contributed to this profile.`} />
        <LayerRow color="#58a6ff" label="Work" value={workTypes.map((item) => `${humanize(item.name)} (${item.count})`).join(", ") || "Still discovering"} detail="These are the kinds of business objects MarkOS recognizes across your apps." />
        <LayerRow color="#56d4dd" label="People and teams" value={people.slice(0, 4).map((item) => item.title ?? item.id).join(", ") || `No ${plural(actor)} found yet`} detail="People are connected to the work they create, own, discuss, or complete." />
        <LayerRow color="#d29922" label="Normal patterns" value={`${learned.length} learned, ${measurements.length - learned.length} still learning`} detail="Patterns come from completed work and change only when new evidence or your correction supports it." />
        <LayerRow color="#bc8cff" label="Monitoring" value={`${watchCount} active checks`} detail="MarkOS compares current activity with your profile and asks when a business change is ambiguous." />
      </div>
    </section>

    <section className="mt-8">
      <div className="mb-3 flex items-end justify-between"><div><h2 className="m-0 text-[15px] font-semibold text-ink">Learned patterns</h2><p className="mb-0 mt-1 text-[12.5px] text-muted">Select a pattern to inspect every source record behind the calculation.</p></div></div>
      <div className="grid border border-edge bg-panel md:grid-cols-3">
        {measurements.map((measurement) => <button key={measurement.metric} onClick={() => setNormOpen(measurement.metric)} className="min-h-[118px] border-b border-edge bg-transparent p-4 text-left last:border-b-0 hover:bg-elevated md:border-b-0 md:border-r md:last:border-r-0">
          <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.8px] text-warn"><Dot color="#d29922" size={6} />{measurement.maturity === "stable" ? "Reliable" : measurement.samples ? "Learning" : "Waiting"}</div>
          <div className="mt-3 text-[13.5px] font-medium text-ink">{metricName(measurement.metric)}</div>
          <div className="mt-1 text-[18px] font-semibold text-ink">{measurement.samples ? formatHours(measurement.typical) : "Not enough data"}</div>
          <div className="mt-1 text-[11.5px] text-subtle">{measurement.samples} completed {measurement.samples === 1 ? "record" : "records"}</div>
        </button>)}
        {!measurements.length && <div className="p-5 text-[13px] text-muted">Learned patterns appear after work begins completing.</div>}
      </div>
    </section>

    {normOpen && <NormModal measurement={activeNorm} onClose={() => setNormOpen(null)} onAsk={onAsk} />}
  </div>;
}

function LayerStat({ label, value, color }: { label: string; value: number; color: string }) {
  return <div className="border-b border-edge px-4 py-3 last:border-b-0 sm:border-b-0 sm:border-r sm:last:border-r-0"><div className="flex items-center gap-2 text-[10.5px] uppercase tracking-[0.7px] text-subtle"><Dot color={color} size={5} />{label}</div><div className="mt-1.5 text-[17px] font-semibold text-ink">{value}</div></div>;
}
