/* Shared UI helpers: API client, toasts, drawers, tabs, sortable tables, jobs, evidence viewer, theme. */
(function () {
  const UI = (window.UI = {});

  UI.esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  UI.num = (n, d = 2) => (n === null || n === undefined || Number.isNaN(n) ? "—" : Number(n).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }));
  UI.money = (n, ccy = "") => (n === null || n === undefined ? "—" : (n < 0 ? "−" : "") + Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + (ccy ? " " + ccy : ""));
  UI.pct = (n, d = 1) => (n === null || n === undefined ? "—" : Number(n).toFixed(d) + "%");
  UI.date = (iso) => (iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "—");
  UI.cls = (n) => (n > 0 ? "pos" : n < 0 ? "neg" : "");
  UI.mmss = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

  UI.toast = (message, kind = "") => {
    let box = document.getElementById("toasts");
    if (!box) { box = document.createElement("div"); box.id = "toasts"; box.setAttribute("role", "status"); box.setAttribute("aria-live", "polite"); document.body.appendChild(box); }
    const el = document.createElement("div");
    el.className = "toast " + kind;
    el.textContent = message;
    box.appendChild(el);
    setTimeout(() => el.remove(), kind === "bad" ? 9000 : 4500);
  };

  UI.api = async (url, opts = {}) => {
    const init = { credentials: "same-origin", method: opts.method || "GET", headers: {} };
    if (opts.json !== undefined) { init.body = JSON.stringify(opts.json); init.headers["Content-Type"] = "application/json"; if (!opts.method) init.method = "POST"; }
    if (opts.form) { init.body = opts.form; if (!opts.method) init.method = "POST"; }
    const res = await fetch(url, init);
    if (res.status === 401) { window.location.href = "/login"; throw new Error("Please sign in."); }
    if (!res.ok) {
      let detail = res.statusText;
      try { const body = await res.json(); detail = Array.isArray(body.detail) ? body.detail.map((d) => d.msg || d).join("; ") : body.detail || detail; } catch (e) { /* not json */ }
      throw new Error(detail);
    }
    const type = res.headers.get("content-type") || "";
    return type.includes("json") ? res.json() : opts.raw ? res : res.text();
  };

  UI.pollJob = (id, onUpdate, interval = 1500) =>
    new Promise((resolve, reject) => {
      const tick = async () => {
        try {
          const job = await UI.api("/api/jobs/" + id);
          if (onUpdate) onUpdate(job);
          if (job.status === "COMPLETED") return resolve(job);
          if (job.status === "FAILED" || job.status === "CANCELLED") return reject(new Error(job.error || job.message || "Job failed"));
          setTimeout(tick, interval);
        } catch (e) { reject(e); }
      };
      tick();
    });

  UI.statusPill = (status) => {
    const map = { COMPLETED: "ok", VALID: "ok", PASS: "ok", APPROVED: "ok", FROZEN: "info", RUNNING: "info", FULLY_BACKTESTABLE: "ok", FAITHFUL_TO_INSTRUCTOR: "ok",
      PARTIALLY_BACKTESTABLE: "warn", WARN: "warn", QUEUED: "", DRAFT: "warn", DISCOVERED: "", SKIPPED: "", AWAITING_PERMISSION: "warn", UNAVAILABLE: "bad", FAILED: "bad", INVALID: "bad", FAIL: "bad",
      NOT_RELIABLY_BACKTESTABLE: "bad", CONTAINS_APPROVED_ASSUMPTIONS: "warn", NOT_A_FAITHFUL_TEST: "bad", USER_AUTHORED_RULES: "info", USER_DEFINED: "info", CANCELLED: "" };
    return `<span class="pill ${map[status] ?? "info"}">${UI.esc(String(status || "").replaceAll("_", " ").toLowerCase())}</span>`;
  };

  /* drawers */
  UI.drawer = {
    open(title, html) {
      let back = document.getElementById("drawerBackdrop");
      if (!back) {
        back = document.createElement("div"); back.id = "drawerBackdrop"; back.className = "drawer-backdrop";
        back.innerHTML = '<aside class="drawer" role="dialog" aria-modal="true" aria-labelledby="drawerTitle"><header><strong id="drawerTitle"></strong><button class="icon-btn" aria-label="Close" id="drawerClose">✕</button></header><div class="body" id="drawerBody"></div></aside>';
        document.body.appendChild(back);
        back.addEventListener("click", (e) => { if (e.target === back) UI.drawer.close(); });
        back.querySelector("#drawerClose").addEventListener("click", UI.drawer.close);
        document.addEventListener("keydown", (e) => { if (e.key === "Escape") UI.drawer.close(); });
      }
      back.querySelector("#drawerTitle").textContent = title;
      back.querySelector("#drawerBody").innerHTML = html;
      back.classList.add("open"); back.querySelector(".drawer").classList.add("open");
      back.querySelector("#drawerClose").focus();
    },
    close() {
      const back = document.getElementById("drawerBackdrop");
      if (back) { back.classList.remove("open"); back.querySelector(".drawer").classList.remove("open"); const body = back.querySelector("#drawerBody"); setTimeout(() => { if (!back.classList.contains("open")) body.innerHTML = ""; }, 250); }
    },
  };

  UI.openEvidence = async (kind, id) => {
    UI.drawer.open("Source evidence", '<div class="skeleton" style="height:260px"></div>');
    try {
      const e = await UI.api(`/api/evidence/${kind}/${encodeURIComponent(id)}`);
      const frames = (e.frames || []).map((f) => `<a href="${UI.esc(f.url)}" target="_blank" rel="noopener"><img src="${UI.esc(f.url)}" alt="Chart frame at ${UI.mmss(f.timestamp)}" loading="lazy"></a>`).join("");
      const prov = e.provenance === "source_confirmed" ? '<span class="pill ok">source-confirmed</span>' : e.provenance === "missing_unclear" ? '<span class="pill warn">quote not verified</span>' : '<span class="pill">transcript</span>';
      const around = (e.transcriptAround || []).map((s) => `<div class="small"><a href="https://www.youtube.com/watch?v=${UI.esc(e.youtubeId)}&t=${Math.floor(s.start)}s" target="_blank" rel="noopener" class="mono">${UI.mmss(s.start)}</a> ${UI.esc(s.text)}</div>`).join("");
      UI.drawer.open(e.video || "Source evidence", `
        <iframe class="video-embed" src="${UI.esc(e.embedUrl)}" title="Source video at ${UI.mmss(e.time)}" allowfullscreen loading="lazy"></iframe>
        <div class="row between" style="margin:.75rem 0"><span class="pill info">at ${UI.mmss(e.time)}</span>${prov}<a href="${UI.esc(e.watchUrl)}" target="_blank" rel="noopener" class="small">Open on YouTube ↗</a></div>
        <p>${UI.esc(e.text)}</p>
        ${e.quote ? `<div class="quote">“${UI.esc(e.quote)}” — verbatim from the transcript</div>` : ""}
        ${e.visualNote ? `<p class="small muted"><strong>Chart:</strong> ${UI.esc(e.visualNote)}</p>` : ""}
        ${frames ? `<h3 class="small" style="margin-top:1rem">Captured chart frames</h3><div class="frame-strip">${frames}</div>` : ""}
        ${around ? `<h3 class="small" style="margin-top:1rem">Transcript around this moment</h3>${around}` : ""}`);
    } catch (err) { UI.drawer.open("Source evidence", `<div class="banner bad">${UI.esc(err.message)}</div>`); }
  };

  /* tabs: <div data-tabs> <button class="tab" data-tab="x"> … <div class="tab-panel" id="x"> */
  UI.tabs = (root = document) => {
    root.querySelectorAll("[data-tabs]").forEach((box) => {
      const buttons = box.querySelectorAll(".tab"), activate = (name) => {
        buttons.forEach((b) => { const on = b.dataset.tab === name; b.classList.toggle("active", on); b.setAttribute("aria-selected", on); });
        document.querySelectorAll(box.dataset.tabs + " .tab-panel, .tab-panel[data-group='" + box.dataset.group + "']").forEach((p) => p.classList.toggle("active", p.id === name));
        box.dispatchEvent(new CustomEvent("tabchange", { detail: name }));
        if (box.dataset.hash) history.replaceState(null, "", "#" + name);
      };
      buttons.forEach((b) => b.addEventListener("click", () => activate(b.dataset.tab)));
      const wanted = (box.dataset.hash && location.hash.slice(1)) || (box.querySelector(".tab.active") || buttons[0]).dataset.tab;
      activate([...buttons].some((b) => b.dataset.tab === wanted) ? wanted : buttons[0].dataset.tab);
      box.activate = activate;
    });
  };

  UI.sortable = (table) => {
    const body = table.tBodies[0];
    table.querySelectorAll("th[data-sort]").forEach((th, idx) => {
      th.addEventListener("click", () => {
        const dir = th.classList.contains("sorted-asc") ? -1 : 1, col = [...th.parentNode.children].indexOf(th);
        table.querySelectorAll("th").forEach((x) => x.classList.remove("sorted-asc", "sorted-desc"));
        th.classList.add(dir === 1 ? "sorted-asc" : "sorted-desc");
        const rows = [...body.rows].sort((a, b) => {
          const x = a.cells[col].dataset.v ?? a.cells[col].textContent, y = b.cells[col].dataset.v ?? b.cells[col].textContent;
          const nx = parseFloat(x), ny = parseFloat(y);
          return (Number.isNaN(nx) || Number.isNaN(ny) ? String(x).localeCompare(String(y)) : nx - ny) * dir;
        });
        rows.forEach((r) => body.appendChild(r));
      });
    });
  };

  UI.copy = async (text) => { try { await navigator.clipboard.writeText(text); UI.toast("Copied", "ok"); } catch (e) { UI.toast("Copy failed", "bad"); } };
  UI.download = (name, text, type = "text/plain") => { const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([text], { type })); a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 2000); };

  /* theme */
  UI.setTheme = (t) => { document.documentElement.dataset.theme = t; try { localStorage.setItem("theme", t); } catch (e) { /* private mode */ } };
  UI.toggleTheme = () => UI.setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");

  /* global add-source modal */
  window.openIngestModal = () => { document.getElementById("ingestModal")?.classList.add("active"); document.getElementById("youtubeUrlInput")?.focus(); };
  window.closeIngestModal = () => document.getElementById("ingestModal")?.classList.remove("active");
  window.previewIngest = async () => {
    const input = document.getElementById("youtubeUrlInput"), box = document.getElementById("ingestPreview");
    if (!input.value.trim()) { box.innerHTML = ""; return; }
    try {
      const p = await UI.api("/api/sources/preview", { json: { url: input.value.trim() } });
      box.innerHTML = `<div class="banner info" style="margin:0">${p.thumbnail ? `<img src="${UI.esc(p.thumbnail)}" alt="" style="height:54px;border-radius:4px;float:left;margin-right:.75rem">` : ""}<strong>${UI.esc(p.kind)}</strong> detected. ${UI.esc(p.note)}</div>`;
    } catch (e) { box.innerHTML = `<div class="banner bad" style="margin:0">${UI.esc(e.message)}</div>`; }
  };
  window.handleIngestSubmit = async (event) => {
    event.preventDefault();
    const status = document.getElementById("ingestStatus"), url = document.getElementById("youtubeUrlInput").value.trim();
    status.textContent = "Queueing…";
    try {
      const r = await UI.api("/api/sources", { json: { url } });
      UI.toast(r.message, "ok"); window.location.href = "/sources";
    } catch (e) { status.textContent = e.message; status.style.color = "var(--bad)"; }
  };

  document.addEventListener("DOMContentLoaded", () => {
    UI.tabs();
    document.querySelectorAll("table[data-sortable]").forEach(UI.sortable);
    document.querySelectorAll("[data-copy]").forEach((b) => b.addEventListener("click", () => UI.copy(b.dataset.copy)));
    document.getElementById("themeToggle")?.addEventListener("click", UI.toggleTheme);
    document.getElementById("logoutBtn")?.addEventListener("click", async () => { await UI.api("/api/auth/logout", { method: "POST" }); window.location.href = "/login"; });
    document.getElementById("youtubeUrlInput")?.addEventListener("change", window.previewIngest);
    document.addEventListener("click", (e) => { const b = e.target.closest("[data-evidence]"); if (b) { e.preventDefault(); UI.openEvidence(b.dataset.kind || "atom", b.dataset.evidence); } });
  });
})();
