const stats = [
  ["Videos processed", "0"], ["Detected trades", "0"], ["Validated trades", "0"], ["AI cost", "$0.00"],
];

export default function Dashboard() {
  return (
    <main className="min-h-screen bg-[#080b10] text-slate-100">
      <div className="mx-auto max-w-7xl px-6 py-10">
        <div className="mb-10 flex items-end justify-between gap-6">
          <div><p className="text-xs uppercase tracking-[.24em] text-emerald-400">Research workspace</p><h1 className="mt-2 text-3xl font-semibold">Trader Video Intelligence</h1><p className="mt-2 max-w-2xl text-sm text-slate-400">Evidence-first reconstruction of observable Forex trading decisions from video, transcript, and chart context.</p></div>
          <span className="rounded-full border border-slate-800 bg-slate-900 px-3 py-1.5 text-xs text-slate-400">Phase 1 foundation</span>
        </div>
        <section className="grid gap-4 md:grid-cols-4">{stats.map(([label, value]) => <div key={label} className="rounded-xl border border-slate-800 bg-slate-950 p-5"><p className="text-sm text-slate-500">{label}</p><p className="mt-3 text-2xl font-semibold">{value}</p></div>)}</section>
        <section className="mt-6 grid gap-6 lg:grid-cols-[1.4fr_.6fr]">
          <div className="rounded-xl border border-slate-800 bg-slate-950 p-6"><h2 className="font-medium">Processing queue</h2><div className="mt-8 rounded-lg border border-dashed border-slate-800 px-6 py-12 text-center text-sm text-slate-500">No videos queued yet. Playlist and video ingestion arrives in Phase 2.</div></div>
          <div className="rounded-xl border border-slate-800 bg-slate-950 p-6"><h2 className="font-medium">Evidence policy</h2><div className="mt-5 space-y-3 text-sm text-slate-400"><p>Direct observations remain separate from spoken claims.</p><p>Inference is explicitly labeled and confidence-scored.</p><p>Unavailable prices, SL, TP, reasons, or outcomes remain unknown.</p></div></div>
        </section>
      </div>
    </main>
  );
}
