"use client";

import { useMemo, useState } from "react";
import { CLUSTERS, COLLECTIONS, cx, REGION_LABEL } from "./data";
import { Chip, Label, Logo, Mono, useToast } from "./ui/kit";
import { Overview } from "./views/overview";
import { Collections } from "./views/collections";
import { Upload } from "./views/upload";
import { Query } from "./views/query";
import { Keys } from "./views/keys";
import { Sdk } from "./views/sdk";
import { Traces } from "./views/traces";

type Section = "overview" | "collections" | "upload" | "query" | "keys" | "sdk" | "traces";

const NAV: { id: Section; label: string; icon: string; group: string }[] = [
  { id: "overview", label: "Overview", icon: "M3 3h7v7H3zM14 3h7v4h-7zM14 10h7v11h-7zM3 14h7v7H3z", group: "Cluster" },
  { id: "collections", label: "Collections", icon: "M4 5c0-1.1 3.6-2 8-2s8 .9 8 2-3.6 2-8 2-8-.9-8-2zM4 5v14c0 1.1 3.6 2 8 2s8-.9 8-2V5", group: "Cluster" },
  { id: "upload", label: "Upload data", icon: "M12 15V4m0 0L8 8m4-4l4 4M4 17v2a1 1 0 001 1h14a1 1 0 001-1v-2", group: "Data" },
  { id: "query", label: "Query & chat", icon: "M21 12a9 9 0 01-9 9 9 9 0 01-4-1l-4 1 1-4a9 9 0 1116-5z", group: "Data" },
  { id: "traces", label: "Traces", icon: "M3 12h4l3 8 4-16 3 8h4", group: "Data" },
  { id: "keys", label: "API keys", icon: "M15 7a4 4 0 11-3.8 5.3L7 16.5 5 15l1.5-2L4 11l2-2 3.2 3.2A4 4 0 0115 7z", group: "Develop" },
  { id: "sdk", label: "SDK & docs", icon: "M8 9l-4 3 4 3m8-6l4 3-4 3M13 5l-2 14", group: "Develop" },
];

export default function Page() {
  const [section, setSection] = useState<Section>("overview");
  const [clusterId, setClusterId] = useState(CLUSTERS[0].id);
  const [collectionId, setCollectionId] = useState<string | null>(null);
  const [switcher, setSwitcher] = useState(false);
  const { toast, node } = useToast();

  const cluster = CLUSTERS.find((c) => c.id === clusterId)!;
  const collections = useMemo(
    () => COLLECTIONS.filter((c) => c.clusterId === clusterId),
    [clusterId]
  );

  const groups = ["Cluster", "Data", "Develop"];

  return (
    <main className="flex h-screen overflow-hidden bg-canvas text-ink">
      {/* Rail */}
      <aside className="flex w-56 shrink-0 flex-col border-r border-edge bg-panel">
        <div className="flex h-14 items-center border-b border-edge px-4">
          <Logo />
        </div>

        <nav className="flex-1 space-y-5 overflow-y-auto px-3 py-4">
          {groups.map((g) => (
            <div key={g}>
              <Label className="px-2">{g}</Label>
              <div className="mt-1.5 space-y-0.5">
                {NAV.filter((n) => n.group === g).map((n) => (
                  <button
                    key={n.id}
                    onClick={() => setSection(n.id)}
                    className={cx(
                      "flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-xs font-medium transition",
                      section === n.id
                        ? "bg-accent/10 text-ink"
                        : "text-muted hover:bg-elevated hover:text-ink"
                    )}
                  >
                    <svg
                      width="15"
                      height="15"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke={section === n.id ? "#7c8cff" : "currentColor"}
                      strokeWidth="1.7"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    >
                      <path d={n.icon} />
                    </svg>
                    {n.label}
                    {section === n.id && (
                      <span className="ml-auto h-1.5 w-1.5 rounded-full bg-accent" />
                    )}
                  </button>
                ))}
              </div>
            </div>
          ))}
        </nav>

        <div className="border-t border-edge p-3">
          <div className="flex items-center gap-2 rounded-lg bg-elevated px-2.5 py-2">
            <span className="flex h-6 w-6 items-center justify-center rounded-md bg-accent/20 font-mono text-2xs text-accentSoft">
              HK
            </span>
            <div className="min-w-0">
              <div className="truncate text-2xs text-ink">harish.temprl</div>
              <div className="truncate font-mono text-2xs text-subtle">team · pro</div>
            </div>
          </div>
        </div>
      </aside>

      {/* Main column */}
      <div className="flex min-w-0 flex-1 flex-col">
        {/* Top bar: the cluster context switcher */}
        <header className="relative flex h-14 shrink-0 items-center gap-3 border-b border-edge px-5">
          <button
            onClick={() => setSwitcher((s) => !s)}
            className="flex items-center gap-2.5 rounded-lg border border-edge bg-elevated px-3 py-1.5 transition hover:border-edgeStrong"
          >
            <span
              className={cx(
                "h-2 w-2 rounded-full",
                cluster.status === "healthy"
                  ? "bg-success"
                  : cluster.status === "provisioning"
                    ? "bg-accentSoft animate-pulse"
                    : "bg-danger"
              )}
            />
            <Mono className="text-xs font-medium text-ink">{cluster.name}</Mono>
            <Label>{cluster.region}</Label>
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="text-subtle">
              <path d="M6 9l6 6 6-6" />
            </svg>
          </button>

          {switcher && (
            <>
              <div className="fixed inset-0 z-20" onClick={() => setSwitcher(false)} />
              <div className="absolute left-5 top-14 z-30 w-72 card-in rounded-xl border border-edgeStrong bg-raised p-1.5 shadow-2xl shadow-black/50">
                <Label className="px-2 py-1">Switch cluster</Label>
                {CLUSTERS.map((c) => (
                  <button
                    key={c.id}
                    onClick={() => {
                      setClusterId(c.id);
                      setCollectionId(null);
                      setSwitcher(false);
                    }}
                    className={cx(
                      "flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left transition",
                      c.id === clusterId ? "bg-accent/10" : "hover:bg-elevated"
                    )}
                  >
                    <span
                      className={cx(
                        "h-2 w-2 shrink-0 rounded-full",
                        c.status === "healthy" ? "bg-success" : c.status === "provisioning" ? "bg-accentSoft" : "bg-danger"
                      )}
                    />
                    <div className="min-w-0 flex-1">
                      <Mono className="block truncate text-xs text-ink">{c.name}</Mono>
                      <span className="text-2xs text-subtle">{REGION_LABEL[c.region]}</span>
                    </div>
                    <Chip>{c.tier}</Chip>
                  </button>
                ))}
              </div>
            </>
          )}

          <div className="ml-auto flex items-center gap-2">
            <Chip tone="text-muted border-edgeStrong bg-elevated">
              <span className="mr-1 h-1.5 w-1.5 rounded-full bg-heat-2" />
              {cluster.endpoint}
            </Chip>
          </div>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto">
          {section === "overview" && (
            <Overview
              cluster={cluster}
              collections={collections}
              onOpen={(id) => {
                setCollectionId(id);
                setSection("collections");
              }}
              go={setSection}
            />
          )}
          {section === "collections" && (
            <Collections
              collections={collections}
              selected={collectionId}
              onSelect={setCollectionId}
              onQuery={() => setSection("query")}
            />
          )}
          {section === "upload" && <Upload collections={collections} toast={toast} />}
          {section === "query" && <Query collections={collections} />}
          {section === "keys" && <Keys toast={toast} />}
          {section === "sdk" && <Sdk cluster={cluster} collections={collections} />}
          {section === "traces" && <Traces cluster={cluster} collections={collections} />}
        </div>
      </div>
      {node}
    </main>
  );
}
