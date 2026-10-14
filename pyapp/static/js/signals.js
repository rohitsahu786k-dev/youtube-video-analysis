(() => {
  const panel = document.getElementById("live-analysis");
  if (!panel) return;
  const intervals = { "1": "M1", "5": "M5", "15": "M15", "30": "M30", "60": "H1", "240": "H4", "D": "D" };
  const status = document.getElementById("signal-status");
  const results = document.getElementById("signal-results");
  const button = document.getElementById("refresh-signals");
  let busy = false;
  let expiry;
  async function refresh() {
    if (busy || document.hidden) return;
    clearTimeout(expiry);
    results.replaceChildren();
    if (!intervals[panel.dataset.interval]) { status.textContent = "WAIT: Live analysis supports intraday and daily timeframes."; return; }
    busy = true; button.disabled = true;
    status.textContent = "Checking current market and tested rules…";
    try {
      const params = new URLSearchParams({ symbol: panel.dataset.symbol, timeframe: intervals[panel.dataset.interval] });
      const response = await fetch(`/api/signals?${params}`, { signal: AbortSignal.timeout(35000) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Live analysis unavailable");
      status.textContent = `${data.status}: ${data.reason || (data.status === "PAPER_SETUP" ? "A tested rule matched; review the source and current price." : "No eligible setup right now.")}`;
      for (const signal of data.signals || []) {
        const row = document.createElement("p");
        row.className = "chart-note";
        row.textContent = signal.status === "PAPER_SETUP" ? `${signal.direction} · Entry quote ${signal.entry} · Stop ${signal.stop} · Target ${signal.target} · ${signal.reason} · Quote ${signal.quoteTime}` : `WAIT: ${signal.reason}`;
        results.appendChild(row);
      }
      const validTimes = (data.signals || []).filter(s => s.status === "PAPER_SETUP" && s.expiresAt).map(s => s.expiresAt * 1000 - Date.now());
      const ttl = validTimes.length ? Math.max(0, Math.min(60000, ...validTimes)) : 60000;
      expiry = setTimeout(() => { results.replaceChildren(); status.textContent = "WAIT: Previous analysis expired. Check again for a fresh quote."; }, ttl);
    } catch (error) { status.textContent = `WAIT: ${error.message}`; }
    finally { busy = false; button.disabled = false; }
  }
  button.addEventListener("click", refresh);
  refresh();
  setInterval(refresh, 60000);
})();
