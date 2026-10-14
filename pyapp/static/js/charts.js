(() => {
  const container = document.getElementById("tradingview-chart");
  const config = document.getElementById("tradingview-config");
  if (!container || !config) return;

  const script = document.createElement("script");
  script.src = "https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js";
  script.type = "text/javascript";
  script.async = true;
  script.textContent = JSON.stringify(JSON.parse(config.textContent));
  script.addEventListener("error", () => {
    document.getElementById("chart-error").hidden = false;
  });
  container.appendChild(script);
})();
