"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import * as d3 from "d3";
import { HEAT_HEX, simBand } from "../data";

export type GraphNode = {
  id: string;
  title: string;
  source: string;
  degree: number;
  category: string | null;
  categoryName: string | null;
  itemId?: string;
  ordinal?: number;
  document?: string;
  nodeType?: string;
  stage?: number;
  px?: number | null;
  py?: number | null;
};

export type GraphEdge = {
  src: string;
  dst: string;
  similarity: number;
  kind: string;
};

type SimNode = GraphNode & d3.SimulationNodeDatum & { radius: number; colour: string };
type SimLink = d3.SimulationLinkDatum<SimNode> & GraphEdge;

const COLOURS = ["#7c8cff", "#4fd6c9", "#c6f45f", "#ff9d5c", "#f5c451", "#ff7a7a", "#9aa6ff"];

function groupKey(node: GraphNode): string {
  return node.itemId || node.category || node.source || "unfiled";
}

export function CollectionGraph({
  nodes,
  edges,
  mode = "graph",
  selectedId = null,
  onSelect,
  onOpen,
}: {
  nodes: GraphNode[];
  edges: GraphEdge[];
  mode?: "graph" | "map";
  selectedId?: string | null;
  onSelect?: (id: string | null) => void;
  onOpen?: (id: string) => void;
}) {
  const frameRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const selectedRef = useRef(selectedId);
  const selectRef = useRef(onSelect);
  const openRef = useRef(onOpen);
  const [size, setSize] = useState({ width: 1100, height: 650 });

  selectedRef.current = selectedId;
  selectRef.current = onSelect;
  openRef.current = onOpen;

  const groups = useMemo(() => {
    const keys = [...new Set(nodes.map(groupKey))];
    return new Map(keys.map((key, index) => [key, index]));
  }, [nodes]);

  useEffect(() => {
    if (!frameRef.current) return;
    const observer = new ResizeObserver(([entry]) => {
      const width = Math.max(520, Math.round(entry.contentRect.width));
      const height = Math.max(560, Math.min(760, Math.round(window.innerHeight * 0.68)));
      setSize({ width, height });
    });
    observer.observe(frameRef.current);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!svgRef.current) return;
    const svg = d3.select<SVGSVGElement, unknown>(svgRef.current);
    svg.selectAll("*").remove();
    if (!nodes.length) return;

    const { width, height } = size;
    const maxDegree = Math.max(1, ...nodes.map((node) => node.degree));
    const simulationNodes: SimNode[] = nodes.map((node, index) => {
      const angle = index * Math.PI * (3 - Math.sqrt(5));
      const spread = Math.sqrt(index / Math.max(1, nodes.length)) * Math.min(width, height) * 0.3;
      return {
        ...node,
        x: width / 2 + Math.cos(angle) * spread,
        y: height / 2 + Math.sin(angle) * spread,
        radius: 5 + (node.degree / maxDegree) * 7,
        colour: COLOURS[(groups.get(groupKey(node)) ?? 0) % COLOURS.length],
      };
    });
    const byId = new Map(simulationNodes.map((node) => [node.id, node]));
    const simulationLinks: SimLink[] = edges
      .filter((edge) => byId.has(edge.src) && byId.has(edge.dst))
      .map((edge) => ({ ...edge, source: edge.src, target: edge.dst }));

    const viewport = svg.append("g").attr("class", "graph-viewport");
    const linkLayer = viewport.append("g").attr("class", "links");
    const nodeLayer = viewport.append("g").attr("class", "nodes");

    const link = linkLayer
      .selectAll<SVGLineElement, SimLink>("line")
      .data(simulationLinks)
      .join("line")
      .attr("stroke", (edge) =>
        edge.kind === "same_document" ? "#3a4250" : HEAT_HEX[simBand(edge.similarity)]
      )
      .attr("stroke-width", (edge) => edge.kind === "same_document" ? 1 : 0.6 + edge.similarity * 1.6)
      .attr("stroke-dasharray", (edge) => edge.kind === "same_document" ? "2 4" : null)
      .attr("stroke-opacity", (edge) => edge.kind === "same_document" ? 0.48 : 0.2 + edge.similarity * 0.42);

    const node = nodeLayer
      .selectAll<SVGGElement, SimNode>("g")
      .data(simulationNodes, (entry) => entry.id)
      .join((enter) => {
        const group = enter
          .append("g")
          .attr("data-id", (entry) => entry.id)
          .attr("tabindex", 0)
          .attr("role", "button")
          .attr("aria-label", (entry) => entry.title || `Passage ${entry.ordinal ?? 0}`)
          .style("cursor", "pointer")
          .style("opacity", 0);
        group.append("circle").attr("class", "selection-ring").attr("fill", "none");
        group
          .append("circle")
          .attr("class", "node-dot")
          .attr("r", (entry) => entry.radius)
          .attr("fill", (entry) => entry.colour)
          .attr("stroke", "#070809")
          .attr("stroke-width", 1.3);
        group
          .filter((entry) => entry.nodeType === "summary")
          .append("circle")
          .attr("r", (entry) => entry.radius + 3.5)
          .attr("fill", "none")
          .attr("stroke", "#f5c451")
          .attr("stroke-width", 1.4);
        group.append("title").text((entry) => `${entry.document || entry.source}\n${entry.title}`);
        return group;
      });

    node
      .transition()
      .duration(window.matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 420)
      .style("opacity", 1);

    const updateSelection = (id: string | null) => {
      node.attr("opacity", (entry) => !id || entry.id === id ? 1 : 0.24);
      link.attr("stroke-opacity", (entry) => {
        if (!id) return entry.kind === "same_document" ? 0.48 : 0.2 + entry.similarity * 0.42;
        const source = typeof entry.source === "object" ? entry.source.id : String(entry.source);
        const target = typeof entry.target === "object" ? entry.target.id : String(entry.target);
        return source === id || target === id ? 0.9 : 0.035;
      });
      node
        .select<SVGCircleElement>(".selection-ring")
        .attr("r", (entry) => entry.radius + 6)
        .attr("stroke", (entry) => entry.colour)
        .attr("stroke-width", 2)
        .attr("opacity", (entry) => entry.id === id ? 0.9 : 0);
    };
    updateSelection(selectedRef.current);

    node
      .on("click", (event, entry) => {
        event.stopPropagation();
        if (!selectRef.current) {
          openRef.current?.(entry.id);
          return;
        }
        const next = selectedRef.current === entry.id ? null : entry.id;
        selectedRef.current = next;
        updateSelection(next);
        selectRef.current?.(next);
      })
      .on("keydown", (event, entry) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        if (!selectRef.current) {
          openRef.current?.(entry.id);
          return;
        }
        const next = selectedRef.current === entry.id ? null : entry.id;
        selectedRef.current = next;
        updateSelection(next);
        selectRef.current?.(next);
      });

    svg.on("click", () => {
      selectedRef.current = null;
      updateSelection(null);
      selectRef.current?.(null);
    });

    const zoom = d3
      .zoom<SVGSVGElement, unknown>()
      .scaleExtent([0.45, 3.5])
      .filter((event) => !event.button)
      .on("zoom", (event) => viewport.attr("transform", event.transform));
    svg.call(zoom).on("dblclick.zoom", null);

    if (mode === "map") {
      simulationNodes.forEach((entry) => {
        entry.x = 50 + (entry.px ?? 0.5) * (width - 100);
        entry.y = 50 + (1 - (entry.py ?? 0.5)) * (height - 100);
      });
      node.attr("transform", (entry) => `translate(${entry.x},${entry.y})`);
      link
        .attr("x1", (entry) => byId.get(String(entry.source))?.x ?? 0)
        .attr("y1", (entry) => byId.get(String(entry.source))?.y ?? 0)
        .attr("x2", (entry) => byId.get(String(entry.target))?.x ?? 0)
        .attr("y2", (entry) => byId.get(String(entry.target))?.y ?? 0);
      return () => { svg.on(".zoom", null).on("click", null); };
    }

    const clusterCount = Math.max(1, groups.size);
    const clusterRadius = Math.min(width, height) * Math.min(0.28, 0.08 + clusterCount * 0.018);
    const centres = new Map<string, [number, number]>();
    [...groups.keys()].forEach((key, index) => {
      const angle = (index / clusterCount) * Math.PI * 2 - Math.PI / 2;
      centres.set(key, [width / 2 + Math.cos(angle) * clusterRadius, height / 2 + Math.sin(angle) * clusterRadius]);
    });

    const simulation = d3
      .forceSimulation<SimNode>(simulationNodes)
      .alpha(1)
      .alphaDecay(0.025)
      .velocityDecay(0.32)
      .force(
        "link",
        d3
          .forceLink<SimNode, SimLink>(simulationLinks)
          .id((entry) => entry.id)
          .distance((entry) => entry.kind === "same_document" ? 34 : 62 + (1 - entry.similarity) * 95)
          .strength((entry) => entry.kind === "same_document" ? 0.42 : 0.18 + entry.similarity * 0.52)
      )
      .force("charge", d3.forceManyBody<SimNode>().strength(-58).distanceMax(260))
      .force("collision", d3.forceCollide<SimNode>().radius((entry) => entry.radius + 4).iterations(2))
      .force("x", d3.forceX<SimNode>((entry) => centres.get(groupKey(entry))?.[0] ?? width / 2).strength(0.055))
      .force("y", d3.forceY<SimNode>((entry) => centres.get(groupKey(entry))?.[1] ?? height / 2).strength(0.055))
      .force("centre", d3.forceCenter(width / 2, height / 2).strength(0.035))
      .on("tick", () => {
        simulationNodes.forEach((entry) => {
          entry.x = Math.max(24, Math.min(width - 24, entry.x ?? width / 2));
          entry.y = Math.max(24, Math.min(height - 24, entry.y ?? height / 2));
        });
        node.attr("transform", (entry) => `translate(${entry.x},${entry.y})`);
        link
          .attr("x1", (entry) => (entry.source as SimNode).x ?? 0)
          .attr("y1", (entry) => (entry.source as SimNode).y ?? 0)
          .attr("x2", (entry) => (entry.target as SimNode).x ?? 0)
          .attr("y2", (entry) => (entry.target as SimNode).y ?? 0);
      });

    const drag = d3
      .drag<SVGGElement, SimNode>()
      .clickDistance(4)
      .on("start", (event, entry) => {
        if (!event.active) simulation.alphaTarget(0.18).restart();
        entry.fx = entry.x;
        entry.fy = entry.y;
      })
      .on("drag", (event, entry) => {
        entry.fx = event.x;
        entry.fy = event.y;
      })
      .on("end", (event, entry) => {
        if (!event.active) simulation.alphaTarget(0);
        entry.fx = null;
        entry.fy = null;
      });
    node.call(drag);

    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      simulation.stop();
      simulation.tick(260);
      simulation.restart().alpha(0.02).alphaDecay(1);
    }

    return () => {
      simulation.stop();
      svg.on(".zoom", null).on("click", null);
    };
  }, [nodes, edges, mode, size, groups]);

  useEffect(() => {
    const svg = d3.select(svgRef.current);
    svg.selectAll<SVGGElement, SimNode>(".nodes > g")
      .attr("opacity", (entry) => !selectedId || entry.id === selectedId ? 1 : 0.24);
    svg.selectAll<SVGCircleElement, SimNode>(".selection-ring")
      .attr("opacity", (entry) => entry.id === selectedId ? 0.9 : 0);
  }, [selectedId]);

  useEffect(() => {
    const clear = (event: KeyboardEvent) => {
      if (event.key === "Escape") onSelect?.(null);
    };
    window.addEventListener("keydown", clear);
    return () => window.removeEventListener("keydown", clear);
  }, [onSelect]);

  if (!nodes.length) {
    return (
      <div className="flex h-[36rem] items-center justify-center rounded-xl border border-dashed border-edge">
        <p className="text-xs text-subtle">No passages match the current filter.</p>
      </div>
    );
  }

  return (
    <div ref={frameRef} className="relative min-h-[36rem] overflow-hidden rounded-xl border border-edge bg-canvas">
      <svg
        ref={svgRef}
        width={size.width}
        height={size.height}
        viewBox={`0 0 ${size.width} ${size.height}`}
        className="block w-full touch-none"
        aria-label="Interactive passage graph. Drag nodes, pan, or zoom. Press Escape or click the background to clear selection."
      />
      <div className="pointer-events-none absolute bottom-3 left-3 rounded-md border border-edge bg-raised/90 px-2.5 py-1.5 font-mono text-2xs text-subtle backdrop-blur">
        Drag nodes · scroll to zoom · click empty space to clear
      </div>
    </div>
  );
}
