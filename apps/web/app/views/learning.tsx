"use client";

import { LearnPoint, MATURITY_TEXT, Measurement, humanize } from "../lib";

/* One learned number, with the working shown — rendered inside the Knowledge
   screen's modal, not as a page of its own.
   Every number here is followed by the actual records that produced it, which
   ones were discarded, and the line where it starts raising alerts. A person
   can check the maths against work they remember, and disagree with it. That
   is the difference between a number you trust and a number you are asked to
   believe. */

function hours(v: number) {
  if (v < 1) return `${Math.round(v * 60)}m`;
  if (v < 48) return `${v.toFixed(v < 10 ? 2 : 1)}h`;
  return `${(v / 24).toFixed(1)}d`;
}

/* Each measurement on one line, positioned by value. Discarded points are
   hollow, the alert line is red — the whole rule readable at a glance. */
function Strip({ m }: { m: Measurement }) {
  const counted = m.points.filter((p) => p.counted);
  if (!counted.length) return null;
  const max = Math.max(m.alert_above, ...m.points.map((p) => p.hours)) * 1.08;
  const pct = (v: number) => `${Math.min(100, (v / max) * 100)}%`;

  return (
    <div className="relative mt-3 h-11 rounded-md border border-edge bg-elevated">
      {/* alert threshold */}
      <div className="absolute top-0 h-full border-l border-dashed" style={{ left: pct(m.alert_above), borderColor: "#f85149" }}>
        <span className="absolute -top-[1px] left-1 whitespace-nowrap text-[10px]" style={{ color: "#f85149" }}>
          flags above {hours(m.alert_above)}
        </span>
      </div>
      {/* typical */}
      <div className="absolute top-0 h-full border-l" style={{ left: pct(m.typical), borderColor: "#3fb950" }}>
        <span className="absolute -bottom-[1px] left-1 whitespace-nowrap text-[10px] text-accent">
          typical {hours(m.typical)}
        </span>
      </div>
      {m.points.map((p) => (
        <span
          key={p.event_id}
          title={`${p.title} — ${hours(p.hours)}${p.counted ? "" : " (ignored as an outlier)"}`}
          className="absolute top-1/2 h-[9px] w-[9px] -translate-x-1/2 -translate-y-1/2 rounded-full"
          style={{
            left: pct(p.hours),
            background: p.counted ? "#8b949e" : "transparent",
            border: p.counted ? "none" : "1px solid #6a6a6a",
          }}
        />
      ))}
    </div>
  );
}

function PointRow({ p, typical }: { p: LearnPoint; typical: number }) {
  const isTypical = Math.abs(p.hours - typical) < 0.005;
  return (
    <div className="grid items-baseline gap-2.5 border-b border-edge py-[7px] last:border-b-0"
         style={{ gridTemplateColumns: "62px 1fr auto" }}>
      <span className="font-mono text-[12px]" style={{ color: p.counted ? "#e8e8e8" : "#6a6a6a" }}>
        {hours(p.hours)}
      </span>
      <span className="min-w-0 truncate text-[12.5px]" style={{ color: p.counted ? "#8b8b8b" : "#6a6a6a" }}>
        {p.title}
        {p.url && <a href={p.url} target="_blank" rel="noreferrer" className="ml-1.5 font-mono text-[11px]">↗</a>}
      </span>
      <span className="text-[11px] text-subtle">
        {!p.counted ? "ignored" : isTypical ? "← the middle one" : ""}
      </span>
    </div>
  );
}

export function MeasurementCard({ m, onAsk }: { m: Measurement; onAsk: (text: string) => void }) {
  const known = m.samples > 0;
  const ignored = m.points.filter((p) => !p.counted).length;

  return (
    <div className="rounded-lg border border-edge bg-panel px-[18px] py-4">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3">
        <span className="text-[14px] font-semibold">{humanize(m.metric.replace(/_hours$/, ""))}</span>
        <span className="text-[11.5px] text-subtle">
          {known ? `${m.samples} finished ${m.samples === 1 ? "example" : "examples"} · ` : ""}
          {MATURITY_TEXT[m.maturity] ?? m.maturity}
        </span>
      </div>

      {/* the rule, in one line of arithmetic rather than a paragraph */}
      <div className="mt-1 font-mono text-[11.5px] text-subtle">
        {m.measured_from.type} · time from created → <span className="text-muted">{m.finished_when}</span>
        {known && <> · middle of {m.samples}{ignored > 0 && <> · {ignored} outlier{ignored === 1 ? "" : "s"} ignored</>}</>}
      </div>

      {known ? (
        <>
          <Strip m={m} />
          <div className="mt-3">
            {[...m.points].sort((a, b) => a.hours - b.hours).map((p) => (
              <PointRow key={p.event_id} p={p} typical={m.typical} />
            ))}
          </div>
          <button
            onClick={() => onAsk(`Recalculate ${m.metric} — I think it is wrong because `)}
            className="mt-3 border-none bg-transparent p-0 text-[12px] text-muted underline hover:text-ink"
          >
            This looks wrong
          </button>
        </>
      ) : m.maturity === "unmeasurable" ? (
        <div className="mt-2">
          <div className="text-[12.5px] text-muted">
            {m.unmeasurable_reason ?? (
              <>
                {"`"}<span className="font-mono text-[12px]">{m.finished_when}</span>{"`"} has never been
                set on anything we've read for this — the source may not send it, or the profile may be
                pointing at the wrong field.
              </>
            )}
          </div>
          <button
            onClick={() => onAsk(`The ${m.metric} field mapping looks wrong — the real field for this is `)}
            className="mt-3 border-none bg-transparent p-0 text-[12px] text-muted underline hover:text-ink"
          >
            Tell the agent which field is right
          </button>
        </div>
      ) : (
        <div className="mt-2 text-[12.5px] text-muted">
          Nothing has finished yet, so there is nothing to measure. This fills in on its own
          once <span className="font-mono text-[12px]">{m.finished_when}</span> starts getting set.
        </div>
      )}
    </div>
  );
}
