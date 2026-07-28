"use client";

import { useEffect, useState } from "react";
import { TaxonomyClass, api } from "../lib";
import { Button, Chip, Empty, Spinner } from "../ui/kit";

type Row = {
  item_id: string;
  title: string;
  class_id: string;
  confidence: number;
  basis: string | null;
};

/** What the knowledge base could not file with confidence.
 *
 *  This screen is the reason low-confidence filing is safe: nothing is
 *  quietly guessed into a category, and nothing is dropped — it lands here
 *  and waits for a person. */
export function Review({
  taxonomy,
  onOpen,
  onChanged,
}: {
  taxonomy: TaxonomyClass[];
  onOpen: (id: string) => void;
  onChanged: () => void;
}) {
  const [rows, setRows] = useState<Row[] | null>(null);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);

  async function load() {
    const res = await api<{ items: Row[] }>("/api/review");
    setRows(res.items);
    setPicked(new Set());
  }

  useEffect(() => {
    load();
  }, []);

  async function fileAs(classId: string) {
    if (!picked.size) return;
    setBusy(true);
    try {
      await api(`/api/items/${Array.from(picked)[0]}/classes`, {
        method: "PUT",
        body: JSON.stringify({
          class_ids: [classId],
          item_ids: Array.from(picked),
        }),
      });
      await load();
      onChanged();
    } finally {
      setBusy(false);
    }
  }

  if (rows === null)
    return (
      <div className="px-6">
        <Spinner />
      </div>
    );

  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
      <div className="mx-auto max-w-3xl">
        <header className="mb-5">
          <h2 className="text-sm font-semibold text-ink">Needs a decision</h2>
          <p className="mt-1 text-xs leading-relaxed text-subtle">
            These could not be filed confidently, so they were left here rather than
            guessed at. Choose a category and it stays — nothing will overwrite it later.
          </p>
        </header>

        {rows.length === 0 ? (
          <Empty
            title="Nothing waiting"
            hint="Everything in the knowledge base has been filed with confidence."
          />
        ) : (
          <>
            {picked.size > 0 && (
              <div className="sticky top-0 z-10 mb-3 flex flex-wrap items-center gap-2 rounded-xl border border-accent/40 bg-accent/10 px-3 py-2.5 backdrop-blur">
                <span className="text-2xs text-accentSoft">
                  {picked.size} selected — file as
                </span>
                {taxonomy
                  .filter((c) => c.class_id !== "unfiled")
                  .slice(0, 8)
                  .map((c) => (
                    <Chip
                      key={c.class_id}
                      tone="text-accentSoft border-accent/40 bg-accent/15"
                      onClick={() => !busy && fileAs(c.class_id)}
                    >
                      {c.name}
                    </Chip>
                  ))}
                <Button onClick={() => setPicked(new Set())} className="ml-auto">
                  Clear
                </Button>
              </div>
            )}

            <ul className="space-y-1.5">
              {rows.map((r) => {
                const on = picked.has(r.item_id);
                return (
                  <li
                    key={r.item_id}
                    className="flex items-start gap-3 rounded-xl border border-edge bg-panel px-4 py-3"
                  >
                    <input
                      type="checkbox"
                      checked={on}
                      onChange={() =>
                        setPicked((p) => {
                          const next = new Set(p);
                          on ? next.delete(r.item_id) : next.add(r.item_id);
                          return next;
                        })
                      }
                      className="mt-1 h-3.5 w-3.5 accent-indigo-500"
                    />
                    <button onClick={() => onOpen(r.item_id)} className="min-w-0 flex-1 text-left">
                      <h3 className="truncate text-sm text-ink">{r.title}</h3>
                      <p className="mt-0.5 text-2xs text-subtle">
                        {r.basis || "No category matched"}
                      </p>
                    </button>
                    <Chip tone="text-warn border-warn/30 bg-warn/10">
                      {r.class_id === "unfiled"
                        ? "unfiled"
                        : `${Math.round(r.confidence * 100)}%`}
                    </Chip>
                  </li>
                );
              })}
            </ul>
          </>
        )}
      </div>
    </div>
  );
}
