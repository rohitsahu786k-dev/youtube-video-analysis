(() => {
  const status = document.getElementById("research-status");
  const proposals = JSON.parse(document.getElementById("rule-proposals").textContent);
  document.querySelectorAll("[data-use-proposal]").forEach(button => button.addEventListener("click", () => {
    const proposal = proposals.find(p => p.id === button.dataset.useProposal);
    const form = document.getElementById("strategy-form");
    form.reset();
    for (const key of ["patternId", "kind", "direction", "timeframe", "lookback", "rewardRisk", "stopBuffer", "maxHoldingBars"]) {
      if (proposal[key] !== null && proposal[key] !== undefined) form.elements[key].value = proposal[key];
    }
    status.textContent = "Draft loaded. Fill missing parameters and verify every condition against its source evidence.";
    form.scrollIntoView({ behavior: "smooth", block: "start" });
  }));
  const submit = async (form, url, body, json = true) => {
    const button = form.querySelector("button[type=submit], button");
    button.disabled = true;
    status.textContent = "Processing request…";
    try {
      const response = await fetch(url, { method: "POST", ...(json ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : { body }) });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail));
      status.textContent = "Saved. Refreshing…";
      window.location.reload();
    } catch (error) {
      status.textContent = error.message;
    } finally { button.disabled = false; }
  };
  document.getElementById("strategy-form").addEventListener("submit", event => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form));
    const selected = form.elements.patternId.selectedOptions[0];
    if (!selected) { status.textContent = "A recurring source pattern is required."; return; }
    submit(form, "/api/strategies", { patternId: values.patternId, symbol: values.symbol || null, timeframe: values.timeframe, reviewed: form.elements.reviewed.checked,
      rule: { kind: values.kind, direction: values.direction, lookback: Number(values.lookback), rewardRisk: Number(values.rewardRisk), stopBuffer: Number(values.stopBuffer), maxHoldingBars: Number(values.maxHoldingBars) } });
  });
  document.getElementById("dataset-form").addEventListener("submit", event => {
    event.preventDefault(); submit(event.currentTarget, "/api/datasets/csv", new FormData(event.currentTarget), false);
  });
  document.getElementById("oanda-form").addEventListener("submit", event => {
    event.preventDefault(); submit(event.currentTarget, "/api/datasets/oanda", { ...Object.fromEntries(new FormData(event.currentTarget)), count: 5000 });
  });
  document.getElementById("backtest-form").addEventListener("submit", event => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(event.currentTarget));
    submit(event.currentTarget, "/api/backtests", { ...values, spread: Number(values.spread), slippage: Number(values.slippage) });
  });
})();
