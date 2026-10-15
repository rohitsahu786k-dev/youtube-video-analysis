/* Small dependency-free SVG charts: line/area with hover, bars, histogram. Colours come from CSS tokens so light/dark both work. */
(function () {
  const NS = "http://www.w3.org/2000/svg";
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  function niceTicks(min, max, count = 5) {
    if (min === max) { max = min + 1; }
    const span = max - min, step0 = span / count, mag = Math.pow(10, Math.floor(Math.log10(step0)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= step0) || 10 * mag;
    const start = Math.floor(min / step) * step, ticks = [];
    for (let v = start; v <= max + step * 0.5; v += step) ticks.push(+v.toFixed(10));
    return ticks;
  }
  function compact(v) {
    const a = Math.abs(v);
    return a >= 1e6 ? (v / 1e6).toFixed(1) + "M" : a >= 1e4 ? (v / 1e3).toFixed(0) + "k" : a >= 100 ? v.toFixed(0) : a >= 1 ? v.toFixed(1).replace(/\.0$/, "") : v.toFixed(2);
  }
  function shell(el, height) {
    el.innerHTML = "";
    const w = Math.max(320, el.clientWidth || 640), svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", `0 0 ${w} ${height}`); svg.setAttribute("role", "img");
    el.appendChild(svg);
    const tip = document.createElement("div"); tip.className = "tip"; el.appendChild(tip);
    return { svg, tip, w, h: height };
  }
  const node = (tag, attrs, parent) => { const n = document.createElementNS(NS, tag); for (const k in attrs) n.setAttribute(k, attrs[k]); parent.appendChild(n); return n; };
  function axes(svg, m, w, h, xs, ys, yFmt, xTicks, xFmt) {
    ys.forEach((t) => {
      const y = m.t + (1 - (t - ys[0]) / (ys[ys.length - 1] - ys[0])) * (h - m.t - m.b);
      node("line", { x1: m.l, x2: w - m.r, y1: y, y2: y, stroke: css("--chart-grid"), "stroke-width": 1 }, svg);
      const txt = node("text", { x: m.l - 6, y: y + 3.5, "text-anchor": "end", fill: css("--chart-axis"), "font-size": 10.5 }, svg); txt.textContent = yFmt(t);
    });
    (xTicks || []).forEach(({ x, label }) => { const t = node("text", { x, y: h - m.b + 15, "text-anchor": "middle", fill: css("--chart-axis"), "font-size": 10.5 }, svg); t.textContent = label; });
  }

  const Charts = {
    /** series: [{name,color,points:[[x,y]],area}] with numeric x (epoch seconds or index). */
    line(el, { series, yFormat = compact, xFormat = null, height = 260, baseline = null }) {
      if (!series.length || !series[0].points.length) { el.innerHTML = '<div class="empty">No data</div>'; return; }
      if (!xFormat) {
        const xs = series.flatMap((s) => s.points.map((p) => p[0])), span = Math.max(...xs) - Math.min(...xs), day = 86400;
        xFormat = span < 10 * day ? (x) => new Date(x * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })
          : span < 400 * day ? (x) => new Date(x * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : (x) => new Date(x * 1000).toLocaleDateString(undefined, { month: "short", year: "2-digit" });
      }
      const { svg, tip, w, h } = shell(el, height), m = { l: 54, r: 14, t: 12, b: 26 };
      const all = series.flatMap((s) => s.points);
      const xmin = Math.min(...all.map((p) => p[0])), xmax = Math.max(...all.map((p) => p[0]));
      let ymin = Math.min(...all.map((p) => p[1])), ymax = Math.max(...all.map((p) => p[1]));
      if (baseline !== null) { ymin = Math.min(ymin, baseline); ymax = Math.max(ymax, baseline); }
      const ys = niceTicks(ymin, ymax, 5), lo = ys[0], hi = ys[ys.length - 1];
      const X = (x) => m.l + ((x - xmin) / (xmax - xmin || 1)) * (w - m.l - m.r), Y = (y) => m.t + (1 - (y - lo) / (hi - lo || 1)) * (h - m.t - m.b);
      const xt = [0, 1, 2, 3, 4].map((i) => { const x = xmin + ((xmax - xmin) * i) / 4; return { x: X(x), label: xFormat(x) }; });
      axes(svg, m, w, h, null, ys, yFormat, xt);
      if (baseline !== null) node("line", { x1: m.l, x2: w - m.r, y1: Y(baseline), y2: Y(baseline), stroke: css("--chart-axis"), "stroke-dasharray": "4 3" }, svg);
      series.forEach((s) => {
        const d = s.points.map((p, i) => (i ? "L" : "M") + X(p[0]).toFixed(1) + " " + Y(p[1]).toFixed(1)).join("");
        if (s.area) node("path", { d: d + `L${X(s.points[s.points.length - 1][0])} ${Y(Math.max(lo, Math.min(0, hi)))}L${X(s.points[0][0])} ${Y(Math.max(lo, Math.min(0, hi)))}Z`, fill: s.color, opacity: 0.12 }, svg);
        node("path", { d, fill: "none", stroke: s.color, "stroke-width": 1.8, "stroke-linejoin": "round" }, svg);
      });
      const cross = node("line", { y1: m.t, y2: h - m.b, stroke: css("--chart-axis"), "stroke-width": 1, opacity: 0 }, svg);
      const dots = series.map((s) => node("circle", { r: 3.5, fill: s.color, opacity: 0 }, svg));
      svg.addEventListener("mousemove", (ev) => {
        const r = svg.getBoundingClientRect(), px = ((ev.clientX - r.left) / r.width) * w, x = xmin + ((px - m.l) / (w - m.l - m.r)) * (xmax - xmin);
        cross.setAttribute("opacity", 1); cross.setAttribute("x1", Math.max(m.l, Math.min(w - m.r, px))); cross.setAttribute("x2", Math.max(m.l, Math.min(w - m.r, px)));
        let html = "";
        series.forEach((s, i) => {
          let best = s.points[0]; for (const p of s.points) if (Math.abs(p[0] - x) < Math.abs(best[0] - x)) best = p;
          dots[i].setAttribute("opacity", 1); dots[i].setAttribute("cx", X(best[0])); dots[i].setAttribute("cy", Y(best[1]));
          html += `<div><i style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${s.color};margin-right:5px"></i>${esc(s.name)}: <b>${yFormat(best[1])}</b></div>`;
          if (i === 0) html = `<div>${xFormat(best[0])}${xmaxIsTime(xmax) ? " " + new Date(best[0] * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : ""}</div>` + html;
        });
        tip.innerHTML = html; tip.style.opacity = 1; tip.style.left = ((Math.max(m.l, Math.min(w - m.r, px)) / w) * 100) + "%"; tip.style.top = "30%";
      });
      svg.addEventListener("mouseleave", () => { tip.style.opacity = 0; cross.setAttribute("opacity", 0); dots.forEach((d) => d.setAttribute("opacity", 0)); });
      if (series.length > 1) { const lg = document.createElement("div"); lg.className = "legend"; lg.innerHTML = series.map((s) => `<span><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join(""); el.appendChild(lg); }
      svg.setAttribute("aria-label", series.map((s) => s.name).join(", ") + " line chart");
    },

    /** items: [{label,value}]; positives and negatives use semantic colours; value labels shown when few bars. */
    bars(el, { items, yFormat = compact, height = 240, colors = null, ariaLabel = "bar chart" }) {
      if (!items.length) { el.innerHTML = '<div class="empty">No data</div>'; return; }
      const { svg, tip, w, h } = shell(el, height), m = { l: 54, r: 10, t: 12, b: items.length > 14 ? 46 : 30 };
      const vals = items.map((i) => i.value ?? 0), ys = niceTicks(Math.min(0, ...vals), Math.max(0, ...vals), 5), lo = ys[0], hi = ys[ys.length - 1];
      const Y = (v) => m.t + (1 - (v - lo) / (hi - lo || 1)) * (h - m.t - m.b), bw = (w - m.l - m.r) / items.length;
      axes(svg, m, w, h, null, ys, yFormat, []);
      node("line", { x1: m.l, x2: w - m.r, y1: Y(0), y2: Y(0), stroke: css("--chart-axis") }, svg);
      const every = Math.ceil(items.length / Math.max(4, Math.floor((w - m.l) / 62)));
      items.forEach((it, i) => {
        const v = it.value ?? 0, bwid = Math.min(bw * 0.72, 56), x = m.l + i * bw + (bw - Math.min(bw * 0.72, 56)) / 2, top = Y(Math.max(v, 0)), hh = Math.max(1, Math.abs(Y(v) - Y(0)));
        const fill = it.color || (colors ? colors[i % colors.length] : v >= 0 ? css("--ok") : css("--bad"));
        const r = node("rect", { x, y: top, width: bwid, height: hh, rx: 2, fill, tabindex: 0, "aria-label": `${it.label}: ${yFormat(v)}` }, svg);
        const show = (ev) => { tip.innerHTML = `${esc(it.label)}: <b>${yFormat(v)}</b>${it.extra ? "<br>" + esc(it.extra) : ""}`; tip.style.opacity = 1; tip.style.left = ((x + bwid / 2) / w) * 100 + "%"; tip.style.top = (top / h) * 100 + "%"; };
        r.addEventListener("mousemove", show); r.addEventListener("focus", show); r.addEventListener("mouseleave", () => (tip.style.opacity = 0)); r.addEventListener("blur", () => (tip.style.opacity = 0));
        if (i % every === 0) {
          const t = node("text", { x: x + bwid / 2, y: h - m.b + 14, "text-anchor": items.length > 14 ? "end" : "middle", fill: css("--chart-axis"), "font-size": 10.5, transform: items.length > 14 ? `rotate(-40 ${x + bwid / 2} ${h - m.b + 14})` : "" }, svg);
          t.textContent = it.label;
        }
      });
      svg.setAttribute("aria-label", ariaLabel);
    },

    /** bins: [{from,to,count}] */
    hist(el, { bins, xFormat = (v) => compact(v), height = 220, ariaLabel = "distribution" }) {
      if (!bins.length) { el.innerHTML = '<div class="empty">No data</div>'; return; }
      Charts.bars(el, { items: bins.map((b) => ({ label: xFormat((b.from + b.to) / 2), value: b.count, color: (b.from + b.to) / 2 >= 0 ? css("--ok") : css("--bad"), extra: `${xFormat(b.from)} to ${xFormat(b.to)}` })), yFormat: (v) => String(Math.round(v)), height, ariaLabel });
    },
  };
  function xmaxIsTime(xmax) { return xmax > 1e9; }
  window.Charts = Charts;
})();
