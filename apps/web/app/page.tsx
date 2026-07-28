"use client";

import { useCallback, useEffect, useState } from "react";
import { ApiError, Facets, TaxonomyClass, api, cx } from "./lib";
import { Detail } from "./ui/detail";
import { Gate, Mark } from "./views/gate";
import { Library } from "./views/library";
import { Review } from "./views/review";
import { Search } from "./views/search";
import { Taxonomy } from "./views/taxonomy";

type View = "library" | "search" | "taxonomy" | "review";
type Me = { email?: string; company_name?: string; company_id?: string };

export default function Page() {
  const [me, setMe] = useState<Me | null | undefined>(undefined);
  const [view, setView] = useState<View>("library");
  const [open, setOpen] = useState<string | null>(null);
  const [facets, setFacets] = useState<Facets | null>(null);
  const [taxonomy, setTaxonomy] = useState<TaxonomyClass[]>([]);
  const [filter, setFilter] = useState<{ class_id?: string; source?: string }>({});
  const [reviewCount, setReviewCount] = useState(0);

  const refresh = useCallback(async () => {
    const [f, t, r] = await Promise.all([
      api<Facets>("/api/facets").catch(() => null),
      api<{ classes: TaxonomyClass[] }>("/api/classes").catch(() => ({ classes: [] })),
      api<{ items: unknown[] }>("/api/review").catch(() => ({ items: [] })),
    ]);
    setFacets(f);
    setTaxonomy(t.classes);
    setReviewCount(r.items.length);
  }, []);

  const check = useCallback(async () => {
    try {
      const who = await api<Me>("/api/auth/me");
      setMe(who);
      refresh();
    } catch (e) {
      setMe(e instanceof ApiError && e.status === 401 ? null : null);
    }
  }, [refresh]);

  useEffect(() => {
    check();
  }, [check]);

  if (me === undefined)
    return (
      <main className="flex min-h-screen items-center justify-center">
        <span className="h-4 w-4 animate-spin rounded-full border-2 border-edgeStrong border-t-accent" />
      </main>
    );

  if (me === null) return <Gate onIn={check} />;

  const nav: { id: View; label: string; badge?: number }[] = [
    { id: "library", label: "Library" },
    { id: "search", label: "Search" },
    { id: "review", label: "Review", badge: reviewCount },
    { id: "taxonomy", label: "Categories" },
  ];

  return (
    <main className="flex h-screen flex-col overflow-hidden">
      <header className="flex shrink-0 items-center gap-4 border-b border-edge px-5 py-3">
        <div className="flex items-center gap-2">
          <Mark />
          <span className="text-sm font-semibold tracking-tight text-ink">
            Knowledge Base
          </span>
        </div>

        <nav className="flex items-center gap-0.5">
          {nav.map((n) => (
            <button
              key={n.id}
              onClick={() => {
                setView(n.id);
                setOpen(null);
              }}
              className={cx(
                "flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium transition",
                view === n.id
                  ? "bg-elevated text-ink"
                  : "text-subtle hover:bg-elevated/60 hover:text-muted"
              )}
            >
              {n.label}
              {!!n.badge && (
                <span className="rounded-full bg-warn/20 px-1.5 text-2xs text-warn">
                  {n.badge}
                </span>
              )}
            </button>
          ))}
        </nav>

        <div className="ml-auto flex items-center gap-4">
          {facets && (
            <span className="text-2xs text-subtle">
              <span className="tabular-nums text-muted">{facets.total}</span> document
              {facets.total === 1 ? "" : "s"}
            </span>
          )}
          <div className="flex items-center gap-2">
            <span className="text-2xs text-subtle">{me.company_name || me.email}</span>
            <button
              onClick={async () => {
                await api("/api/auth/logout", { method: "POST" }).catch(() => {});
                setMe(null);
              }}
              className="text-2xs text-subtle transition hover:text-muted"
            >
              Sign out
            </button>
          </div>
        </div>
      </header>

      <div className="flex min-h-0 flex-1">
        <div className="flex min-w-0 flex-1 flex-col">
          {view === "library" && (
            <Library
              facets={facets}
              filter={filter}
              setFilter={setFilter}
              onOpen={setOpen}
              onIngested={refresh}
            />
          )}
          {view === "search" && <Search onOpen={setOpen} />}
          {view === "review" && (
            <Review taxonomy={taxonomy} onOpen={setOpen} onChanged={refresh} />
          )}
          {view === "taxonomy" && (
            <Taxonomy
              classes={taxonomy}
              facets={facets}
              onChanged={refresh}
              onFilter={(class_id) => {
                setFilter({ class_id });
                setView("library");
              }}
            />
          )}
        </div>

        {open && (
          <>
            <div
              onClick={() => setOpen(null)}
              className="fixed inset-0 z-20 bg-black/40 backdrop-blur-[1px]"
            />
            <Detail
              itemId={open}
              taxonomy={taxonomy}
              onClose={() => setOpen(null)}
              onChanged={refresh}
            />
          </>
        )}
      </div>
    </main>
  );
}
