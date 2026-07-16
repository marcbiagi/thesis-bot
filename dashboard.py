"""
dashboard.py — generates dashboard.html, a self-contained local page with
the experiment scoreboard: equity curves per arm, stat tiles, decision log.

Regenerated automatically at the end of every runner cycle; can also be
rebuilt on demand:  python3 dashboard.py && open dashboard.html
"""

import json
import re
from datetime import datetime
from string import Template

import db
from config import ARMS, INITIAL_CASH, LLM_MODEL, ROOT

OUT_PATH = ROOT / "dashboard.html"

TRADING_DAYS = 252


def _perf_stats(eq: list[float], bench: list[float], rf_annual_pct: float) -> dict:
    """
    Standard portfolio statistics from an equity series, benchmark-relative
    where applicable. Returns None per metric until enough data exists.
    """
    out = {k: None for k in ("total_ret", "cagr", "vol", "sharpe", "sortino",
                             "max_dd", "beta", "alpha", "te", "ir", "corr")}
    n = len(eq)
    if n < 2:
        return out

    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, n)]
    brets = [bench[i] / bench[i - 1] - 1 for i in range(1, min(n, len(bench)))]
    rf_d = rf_annual_pct / 100.0 / TRADING_DAYS
    days = n - 1

    out["total_ret"] = round((eq[-1] / eq[0] - 1) * 100, 2)
    out["cagr"] = round(((eq[-1] / eq[0]) ** (TRADING_DAYS / days) - 1) * 100, 2)

    mean = sum(rets) / len(rets)
    if len(rets) >= 2:
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        sd = var ** 0.5
        out["vol"] = round(sd * TRADING_DAYS ** 0.5 * 100, 2)
        if sd > 0:
            out["sharpe"] = round((mean - rf_d) / sd * TRADING_DAYS ** 0.5, 2)
        downside = [min(0.0, r - rf_d) for r in rets]
        dvar = sum(d ** 2 for d in downside) / (len(rets) - 1)
        if dvar > 0:
            out["sortino"] = round((mean - rf_d) / dvar ** 0.5 * TRADING_DAYS ** 0.5, 2)

    peak, max_dd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        max_dd = min(max_dd, v / peak - 1)
    out["max_dd"] = round(max_dd * 100, 2)

    # Benchmark-relative statistics (need aligned series).
    m = min(len(rets), len(brets))
    if m >= 3:
        r, b = rets[:m], brets[:m]
        mr, mb = sum(r) / m, sum(b) / m
        cov = sum((r[i] - mr) * (b[i] - mb) for i in range(m)) / (m - 1)
        bvar = sum((x - mb) ** 2 for x in b) / (m - 1)
        rvar = sum((x - mr) ** 2 for x in r) / (m - 1)
        if bvar > 0:
            beta = cov / bvar
            out["beta"] = round(beta, 2)
            # Jensen's alpha, annualized.
            out["alpha"] = round(((mr - rf_d) - beta * (mb - rf_d))
                                 * TRADING_DAYS * 100, 2)
        if bvar > 0 and rvar > 0:
            out["corr"] = round(cov / (bvar ** 0.5 * rvar ** 0.5), 2)
        diff = [r[i] - b[i] for i in range(m)]
        md = sum(diff) / m
        dvar = sum((d - md) ** 2 for d in diff) / (m - 1)
        if dvar > 0:
            out["te"] = round(dvar ** 0.5 * TRADING_DAYS ** 0.5 * 100, 2)
            out["ir"] = round(md / dvar ** 0.5 * TRADING_DAYS ** 0.5, 2)
    return out


def collect() -> dict:
    conn = db.connect()

    series = {arm: [] for arm in ARMS}
    rows = conn.execute(
        "SELECT s.arm, s.equity, r.started_utc FROM snapshots s "
        "JOIN runs r ON r.run_id = s.run_id ORDER BY s.id"
    ).fetchall()
    for r in rows:
        series[r["arm"]].append({"t": r["started_utc"][:10], "eq": round(r["equity"], 2)})

    latest = {}
    for arm in ARMS:
        pts = series[arm]
        eq = pts[-1]["eq"] if pts else None
        latest[arm] = {
            "equity": eq,
            "ret_pct": round((eq / INITIAL_CASH - 1) * 100, 2) if eq else None,
            "positions": conn.execute(
                "SELECT COUNT(*) n FROM positions WHERE arm = ?", (arm,)
            ).fetchone()["n"],
        }

    counts = {}
    for r in conn.execute(
            "SELECT arm, signal, COUNT(*) n FROM decisions GROUP BY arm, signal"):
        counts.setdefault(r["arm"], {})[r["signal"]] = r["n"]

    decisions = [dict(r) for r in conn.execute(
        "SELECT d.created_utc, d.arm, d.ticker, d.signal, d.price, "
        "substr(d.rationale, 1, 400) AS rationale, d.error "
        "FROM decisions d ORDER BY d.id DESC LIMIT 60")]

    runs = [dict(r) for r in conn.execute(
        "SELECT run_id, started_utc, status, llm_model, notes "
        "FROM runs ORDER BY run_id DESC LIMIT 15")]

    # --- Latest known price per ticker (most recent decision that had one) --
    last_price = {r["ticker"]: r["price"] for r in conn.execute(
        "SELECT ticker, price FROM decisions d WHERE price IS NOT NULL AND "
        "id = (SELECT MAX(id) FROM decisions x WHERE x.ticker = d.ticker "
        "AND x.price IS NOT NULL)")}

    # SPY never appears in decisions; imply its latest price from the most
    # recent benchmark snapshot (equity minus cash, divided by shares held).
    bench_pos = conn.execute(
        "SELECT qty FROM positions WHERE arm = 'benchmark' LIMIT 1").fetchone()
    bench_snap = conn.execute(
        "SELECT equity, cash FROM snapshots WHERE arm = 'benchmark' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    if bench_pos and bench_snap and bench_pos["qty"] > 0:
        last_price.setdefault("SPY", round(
            (bench_snap["equity"] - bench_snap["cash"]) / bench_pos["qty"], 2))

    # --- Open positions with unrealized P/L ---------------------------------
    positions = []
    for r in conn.execute(
            "SELECT arm, ticker, qty, avg_cost FROM positions ORDER BY arm, ticker"):
        px = last_price.get(r["ticker"])
        mv = r["qty"] * px if px else None
        pl = (px - r["avg_cost"]) * r["qty"] if px else None
        positions.append({
            "arm": r["arm"], "ticker": r["ticker"],
            "qty": round(r["qty"], 4), "avg_cost": round(r["avg_cost"], 2),
            "last": px, "value": round(mv, 2) if mv is not None else None,
            "pl": round(pl, 2) if pl is not None else None,
            "pl_pct": round((px / r["avg_cost"] - 1) * 100, 2) if px else None,
        })

    # --- Research metrics ----------------------------------------------------
    # Agreement between the two deciding arms (same run, same ticker).
    pairs = conn.execute(
        "SELECT a.signal AS lsig, b.signal AS rsig FROM decisions a "
        "JOIN decisions b ON a.run_id = b.run_id AND a.ticker = b.ticker "
        "AND b.arm = 'rules' WHERE a.arm = 'llm' AND a.error IS NULL"
    ).fetchall()
    n_pairs = len(pairs)
    agreement = round(100 * sum(1 for p in pairs if p["lsig"] == p["rsig"])
                      / n_pairs, 1) if n_pairs else None

    # LLM confidence by signal (parsed from the "[conf X]" rationale prefix).
    conf_by_signal = {}
    for r in conn.execute("SELECT signal, rationale FROM decisions "
                          "WHERE arm = 'llm' AND error IS NULL"):
        m = re.match(r"\[conf ([0-9.]+)\]", r["rationale"] or "")
        if m:
            conf_by_signal.setdefault(r["signal"], []).append(float(m.group(1)))
    confidence = {sig: {"avg": round(sum(v) / len(v), 3), "n": len(v)}
                  for sig, v in conf_by_signal.items()}

    # Decision scoreboard: average return since signal, marked at the latest
    # known price — the thesis's decision-level metric, accruing daily.
    fwd = {}
    for arm in ("llm", "rules"):
        fwd[arm] = {}
        for sig in ("BUY", "SELL"):
            rets = [last_price[r["ticker"]] / r["price"] - 1 for r in conn.execute(
                "SELECT ticker, price FROM decisions WHERE arm = ? AND signal = ? "
                "AND error IS NULL AND price > 0", (arm, sig))
                if last_price.get(r["ticker"])]
            fwd[arm][sig] = {"avg_pct": round(100 * sum(rets) / len(rets), 3)
                             if rets else None, "n": len(rets)}

    # Income and executed trades per arm.
    income = {}
    for r in conn.execute("SELECT arm, side, SUM(notional) s FROM trades "
                          "WHERE side IN ('DIV','INT') GROUP BY arm, side"):
        income.setdefault(r["arm"], {})[r["side"]] = round(r["s"], 2)
    exec_trades = {r["arm"]: r["n"] for r in conn.execute(
        "SELECT arm, COUNT(*) n FROM trades WHERE side IN ('BUY','SELL') "
        "GROUP BY arm")}

    lat = conn.execute("SELECT AVG(latency_ms) a, MAX(latency_ms) m FROM "
                       "decisions WHERE arm = 'llm' AND latency_ms IS NOT NULL"
                       ).fetchone()
    research = {
        "agreement_pct": agreement, "n_pairs": n_pairs,
        "confidence": confidence, "fwd": fwd, "income": income,
        "exec_trades": exec_trades,
        "latency_avg_s": round(lat["a"] / 1000, 1) if lat["a"] else None,
        "latency_max_s": round(lat["m"] / 1000, 1) if lat["m"] else None,
    }

    # --- Portfolio performance statistics vs the SPY benchmark ---------------
    rf = float(db.get_kv(conn, "last_tbill_rate") or 4.0)
    bench_eq = [p["eq"] for p in series.get("benchmark", [])]
    perf = {arm: _perf_stats([p["eq"] for p in series.get(arm, [])],
                             bench_eq, rf) for arm in ARMS}
    # The benchmark measured against itself: fix the trivial identities.
    if len(bench_eq) >= 2:
        perf["benchmark"]["beta"] = 1.0
        perf["benchmark"]["alpha"] = 0.0
        perf["benchmark"]["te"] = perf["benchmark"]["ir"] = None
        perf["benchmark"]["corr"] = 1.0
    perf_meta = {"rf_pct": rf, "days": max(0, len(bench_eq) - 1)}

    stats = {
        "total_decisions": conn.execute("SELECT COUNT(*) n FROM decisions").fetchone()["n"],
        "completed_runs": conn.execute(
            "SELECT COUNT(*) n FROM runs WHERE status = 'completed'").fetchone()["n"],
        "skipped_runs": conn.execute(
            "SELECT COUNT(*) n FROM runs WHERE status LIKE 'skipped%'").fetchone()["n"],
        "errors": conn.execute(
            "SELECT COUNT(*) n FROM decisions WHERE error IS NOT NULL").fetchone()["n"],
        "first_run": conn.execute(
            "SELECT MIN(started_utc) t FROM runs WHERE status = 'completed'").fetchone()["t"],
    }

    return {"series": series, "latest": latest, "counts": counts,
            "positions": positions, "research": research,
            "perf": perf, "perf_meta": perf_meta,
            "decisions": decisions, "runs": runs, "stats": stats,
            "model": LLM_MODEL, "initial_cash": INITIAL_CASH,
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M %Z").strip()}


TEMPLATE = Template(r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Thesis Experiment Dashboard</title>
<style>
  :root {
    --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e;
    --muted: #898781; --grid: #e1e0d9; --baseline: #c3c2b7;
    --border: rgba(11,11,11,0.10); --up: #006300; --down: #d03b3b;
    --llm: #2a78d6; --rules: #1baf7a; --benchmark: #898781;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7;
      --muted: #898781; --grid: #2c2c2a; --baseline: #383835;
      --border: rgba(255,255,255,0.10); --up: #0ca30c; --down: #e66767;
      --llm: #3987e5; --rules: #199e70; --benchmark: #898781;
    }
  }
  * { box-sizing: border-box; margin: 0; }
  body { background: var(--page); color: var(--ink); padding: 24px;
         font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; }
  h1 { font-size: 19px; } h2 { font-size: 14px; color: var(--ink-2); margin: 0 0 10px; }
  .sub { color: var(--muted); font-size: 12px; margin: 4px 0 20px; }
  .card { background: var(--surface); border: 1px solid var(--border);
          border-radius: 10px; padding: 16px; margin-bottom: 16px; }
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
           gap: 12px; margin-bottom: 16px; }
  .tile { background: var(--surface); border: 1px solid var(--border);
          border-radius: 10px; padding: 12px 14px; }
  .tile .k { font-size: 11px; color: var(--muted); text-transform: uppercase;
             letter-spacing: .04em; display: flex; align-items: center; gap: 6px; }
  .tile .v { font-size: 22px; font-weight: 650; margin-top: 4px; }
  .tile .d { font-size: 12px; margin-top: 2px; color: var(--ink-2); }
  .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
  .pos { color: var(--up); } .neg { color: var(--down); }
  svg text { font: 11px system-ui, sans-serif; fill: var(--muted); }
  .serieslabel { font-weight: 600; }
  table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  th { text-align: left; color: var(--muted); font-weight: 500; padding: 6px 8px;
       border-bottom: 1px solid var(--grid); white-space: nowrap; }
  td { padding: 6px 8px; border-bottom: 1px solid var(--grid); vertical-align: top; }
  td.num { font-variant-numeric: tabular-nums; text-align: right; }
  .sig { font-weight: 650; padding: 1px 7px; border-radius: 9px; font-size: 11px; }
  .sig.BUY { color: var(--up); background: color-mix(in srgb, var(--up) 12%, transparent); }
  .sig.SELL { color: var(--down); background: color-mix(in srgb, var(--down) 12%, transparent); }
  .sig.HOLD { color: var(--ink-2); background: color-mix(in srgb, var(--muted) 15%, transparent); }
  .armtag { font-size: 11px; color: var(--ink-2); }
  .rationale { color: var(--ink-2); max-width: 560px; }
  .err { color: var(--down); font-size: 11px; }
  .scroll { overflow-x: auto; }
  #tip { position: fixed; pointer-events: none; background: var(--surface);
         border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px;
         font-size: 12px; box-shadow: 0 4px 14px rgba(0,0,0,.15); display: none; z-index: 9; }
  .legend { display: flex; gap: 16px; font-size: 12px; color: var(--ink-2);
            margin-bottom: 8px; flex-wrap: wrap; }
  .legend span { display: inline-flex; align-items: center; gap: 6px; }
  .empty { color: var(--muted); padding: 24px; text-align: center; }
  .metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 18px; }
  .metrics h3 { font-size: 12px; color: var(--muted); text-transform: uppercase;
                letter-spacing: .04em; margin-bottom: 6px; font-weight: 600; }
  .metrics table td:last-child { text-align: right; font-variant-numeric: tabular-nums; }
  .hint { font-weight: 400; font-size: 11px; color: var(--muted); margin-left: 8px; }
  #perf td:not(:first-child) { text-align: right; font-variant-numeric: tabular-nums; }
  #perf th:not(:first-child) { text-align: right; }
</style>
</head>
<body>
<h1>AI vs Rules vs Market — Thesis Experiment</h1>
<div class="sub">Model: <b>$MODEL</b> · generated $GENERATED · each arm starts at
$$100,000 (virtual) · open this file any time: it always shows the latest run</div>

<div class="tiles" id="tiles"></div>

<div class="card">
  <h2>Equity curves</h2>
  <div class="legend" id="legend"></div>
  <div id="chart"></div>
</div>

<div class="card">
  <h2>Open positions</h2>
  <div class="scroll"><table id="pos"></table></div>
</div>

<div class="card">
  <h2>Performance statistics <span class="hint" id="perfhint"></span></h2>
  <div class="scroll"><table id="perf"></table></div>
</div>

<div class="card">
  <h2>Research metrics</h2>
  <div class="metrics" id="metrics"></div>
</div>

<div class="card">
  <h2>Signal mix by arm</h2>
  <div class="scroll"><table id="mix"></table></div>
</div>

<div class="card">
  <h2>Latest decisions (most recent 60)</h2>
  <div class="scroll"><table id="dec"></table></div>
</div>

<div class="card">
  <h2>Run history</h2>
  <div class="scroll"><table id="runs"></table></div>
</div>

<div id="tip"></div>

<script>
const D = $DATA;
const ARMS = [
  {id: "llm",       label: "LLM",       color: "var(--llm)"},
  {id: "rules",     label: "Rules",     color: "var(--rules)"},
  {id: "benchmark", label: "SPY B&H",   color: "var(--benchmark)"},
];
const fmt$$ = v => v == null ? "—" : "$$" + v.toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});
const fmtPct = v => v == null ? "" : (v >= 0 ? "+" : "") + v.toFixed(2) + "%";

/* ---- stat tiles ---- */
const tiles = document.getElementById("tiles");
for (const a of ARMS) {
  const l = D.latest[a.id] || {};
  const cls = (l.ret_pct ?? 0) >= 0 ? "pos" : "neg";
  tiles.insertAdjacentHTML("beforeend", `
    <div class="tile">
      <div class="k"><span class="dot" style="background:${a.color}"></span>${a.label}</div>
      <div class="v">${fmt$$(l.equity)}</div>
      <div class="d"><span class="${cls}">${fmtPct(l.ret_pct)}</span> · ${l.positions ?? 0} positions</div>
    </div>`);
}
const s = D.stats;
const days = s.first_run ? Math.max(1, Math.round((Date.now() - new Date(s.first_run)) / 864e5)) : 0;
tiles.insertAdjacentHTML("beforeend", `
  <div class="tile"><div class="k">Decisions logged</div><div class="v">${s.total_decisions.toLocaleString()}</div>
    <div class="d">${s.errors} with errors</div></div>
  <div class="tile"><div class="k">Runs</div><div class="v">${s.completed_runs}</div>
    <div class="d">${s.skipped_runs} skipped · day ${days} of ~504</div></div>`);

/* ---- legend ---- */
document.getElementById("legend").innerHTML = ARMS.map(a =>
  `<span><span class="dot" style="background:${a.color}"></span>${a.label}</span>`).join("");

/* ---- equity line chart (SVG) ---- */
(function () {
  const host = document.getElementById("chart");
  const all = ARMS.flatMap(a => (D.series[a.id] || []).map(p => p.eq));
  if (!all.length) { host.innerHTML = '<div class="empty">No completed runs yet — the chart appears after the first run.</div>'; return; }

  const W = Math.max(680, Math.min(1100, host.clientWidth || 900)), H = 300;
  const M = {t: 14, r: 90, b: 28, l: 64};
  const n = Math.max(...ARMS.map(a => (D.series[a.id] || []).length));
  let lo = Math.min(...all, D.initial_cash), hi = Math.max(...all, D.initial_cash);
  const pad = Math.max((hi - lo) * 0.08, hi * 0.002); lo -= pad; hi += pad;

  const x = i => M.l + (n === 1 ? (W - M.l - M.r) / 2 : i * (W - M.l - M.r) / (n - 1));
  const y = v => M.t + (H - M.t - M.b) * (1 - (v - lo) / (hi - lo));
  const dates = (D.series[ARMS[0].id] || D.series.rules || []).map(p => p.t);

  let g = "";
  const ticks = 4;
  const kDecimals = (hi - lo) < 5000 ? 1 : 0;   // tight range: 99.8k not 100k
  for (let i = 0; i <= ticks; i++) {
    const v = lo + (hi - lo) * i / ticks, yy = y(v);
    g += `<line x1="${M.l}" y1="${yy}" x2="${W - M.r}" y2="${yy}" stroke="var(--grid)" stroke-width="1"/>
          <text x="${M.l - 8}" y="${yy + 4}" text-anchor="end">${(v / 1000).toFixed(kDecimals)}k</text>`;
  }
  const step = Math.max(1, Math.ceil(n / 6));
  for (let i = 0; i < n; i += step)
    g += `<text x="${x(i)}" y="${H - 8}" text-anchor="middle">${dates[i] || ""}</text>`;
  g += `<line x1="${M.l}" y1="${y(D.initial_cash)}" x2="${W - M.r}" y2="${y(D.initial_cash)}"
         stroke="var(--baseline)" stroke-width="1" stroke-dasharray="4 4"/>`;

  // Draw lines + end markers, then de-collide the direct labels vertically.
  const labels = [];
  for (const a of ARMS) {
    const pts = D.series[a.id] || [];
    if (!pts.length) continue;
    const path = pts.map((p, i) => `${i ? "L" : "M"}${x(i)},${y(p.eq)}`).join("");
    if (pts.length > 1)
      g += `<path d="${path}" fill="none" stroke="${a.color}" stroke-width="2" stroke-linejoin="round"/>`;
    const lastX = x(pts.length - 1), lastY = y(pts[pts.length - 1].eq);
    g += `<circle cx="${lastX}" cy="${lastY}" r="4" fill="${a.color}" stroke="var(--surface)" stroke-width="2"/>`;
    labels.push({label: a.label, color: a.color, y: lastY});
  }
  labels.sort((p, q) => p.y - q.y);
  for (let i = 1; i < labels.length; i++)
    labels[i].y = Math.max(labels[i].y, labels[i - 1].y + 15);
  for (const l of labels)
    g += `<text class="serieslabel" x="${W - M.r + 8}" y="${l.y + 4}" style="fill:${l.color}">${l.label}</text>`;
  g += `<line id="xhair" x1="0" x2="0" y1="${M.t}" y2="${H - M.b}" stroke="var(--baseline)" stroke-width="1" visibility="hidden"/>`;

  host.innerHTML = `<svg id="eqsvg" viewBox="0 0 ${W} ${H}" style="width:100%;max-width:${W}px">${g}</svg>`;

  const svg = document.getElementById("eqsvg"), tip = document.getElementById("tip"),
        xh = document.getElementById("xhair");
  svg.addEventListener("mousemove", ev => {
    const r = svg.getBoundingClientRect(), sx = W / r.width;
    const px = (ev.clientX - r.left) * sx;
    if (px < M.l || px > W - M.r || n < 1) { tip.style.display = "none"; xh.setAttribute("visibility", "hidden"); return; }
    const i = Math.max(0, Math.min(n - 1, Math.round((px - M.l) / ((W - M.l - M.r) / Math.max(1, n - 1)))));
    xh.setAttribute("x1", x(i)); xh.setAttribute("x2", x(i)); xh.setAttribute("visibility", "visible");
    tip.innerHTML = `<b>${dates[i] || ""}</b><br>` + ARMS.map(a => {
      const p = (D.series[a.id] || [])[i];
      return p ? `<span class="dot" style="background:${a.color}"></span> ${a.label}: ${fmt$$(p.eq)}` : "";
    }).filter(Boolean).join("<br>");
    tip.style.display = "block";
    tip.style.left = Math.min(ev.clientX + 14, innerWidth - 200) + "px";
    tip.style.top = (ev.clientY + 14) + "px";
  });
  svg.addEventListener("mouseleave", () => { tip.style.display = "none"; xh.setAttribute("visibility", "hidden"); });
})();

/* ---- open positions ---- */
const armName = id => (ARMS.find(a => a.id === id) || {label: id}).label;
const armColor = id => (ARMS.find(a => a.id === id) || {color: "var(--muted)"}).color;
document.getElementById("pos").innerHTML =
  "<tr><th>Arm</th><th>Ticker</th><th>Qty</th><th>Avg cost</th><th>Last</th><th>Value</th><th>P/L $</th><th>P/L %</th></tr>" +
  (D.positions.length ? D.positions.map(p => {
    const cls = (p.pl ?? 0) >= 0 ? "pos" : "neg";
    return `<tr>
      <td><span class="dot" style="background:${armColor(p.arm)}"></span> ${armName(p.arm)}</td>
      <td><b>${p.ticker}</b></td>
      <td class="num">${p.qty}</td>
      <td class="num">${"$" + p.avg_cost.toFixed(2)}</td>
      <td class="num">${p.last ? "$" + p.last.toFixed(2) : "—"}</td>
      <td class="num">${p.value ? "$" + p.value.toLocaleString("en-US", {minimumFractionDigits: 2}) : "—"}</td>
      <td class="num ${cls}">${p.pl == null ? "—" : (p.pl >= 0 ? "+" : "") + p.pl.toFixed(2)}</td>
      <td class="num ${cls}">${p.pl_pct == null ? "—" : fmtPct(p.pl_pct)}</td></tr>`;
  }).join("") : '<tr><td colspan="8" class="empty">No open positions.</td></tr>');

/* ---- performance statistics ---- */
(function () {
  const P = D.perf, M = D.perf_meta;
  document.getElementById("perfhint").textContent =
    `${M.days} trading day${M.days === 1 ? "" : "s"} of data · risk-free (13w T-bill): ${M.rf_pct}% · annualized where noted`;
  const ROWS = [
    ["total_ret", "Total return", "%"],
    ["cagr", "CAGR (ann.)", "%"],
    ["vol", "Volatility (ann.)", "%"],
    ["sharpe", "Sharpe ratio", ""],
    ["sortino", "Sortino ratio", ""],
    ["max_dd", "Max drawdown", "%"],
    ["beta", "Beta vs SPY", ""],
    ["alpha", "Jensen's alpha (ann.)", "%"],
    ["te", "Tracking error (ann.)", "%"],
    ["ir", "Information ratio", ""],
    ["corr", "Correlation vs SPY", ""],
  ];
  const cell = (arm, key, unit) => {
    const v = P[arm] ? P[arm][key] : null;
    if (v == null) return "<td>—</td>";
    const signed = ["total_ret", "cagr", "alpha"].includes(key);
    const cls = signed ? (v >= 0 ? "pos" : "neg") : (key === "max_dd" && v < 0 ? "neg" : "");
    const txt = (signed && v > 0 ? "+" : "") + v.toFixed(2) + unit;
    return `<td class="${cls}">${txt}</td>`;
  };
  document.getElementById("perf").innerHTML =
    "<tr><th>Metric</th><th>LLM</th><th>Rules</th><th>SPY B&H</th></tr>" +
    ROWS.map(([key, label, unit]) =>
      `<tr><td>${label}</td>${cell("llm", key, unit)}${cell("rules", key, unit)}${cell("benchmark", key, unit)}</tr>`
    ).join("");
})();

/* ---- research metrics ---- */
(function () {
  const R = D.research;
  const pct = v => v == null ? "—" : (v >= 0 ? "+" : "") + v.toFixed(2) + "%";
  const conf = s => R.confidence[s] ? `${R.confidence[s].avg.toFixed(2)} (n=${R.confidence[s].n})` : "—";
  const f = (arm, sig) => {
    const x = R.fwd[arm] && R.fwd[arm][sig];
    return x && x.avg_pct != null ? `${pct(x.avg_pct)} (n=${x.n})` : "—";
  };
  const inc = arm => {
    const i = R.income[arm] || {};
    return "$" + (((i.DIV || 0) + (i.INT || 0)).toFixed(2));
  };
  document.getElementById("metrics").innerHTML = `
    <div><h3>Decision quality</h3><table>
      <tr><td>LLM ↔ Rules agreement</td><td>${R.agreement_pct == null ? "—" : R.agreement_pct + "%"} (n=${R.n_pairs})</td></tr>
      <tr><td>LLM confidence · BUY</td><td>${conf("BUY")}</td></tr>
      <tr><td>LLM confidence · HOLD</td><td>${conf("HOLD")}</td></tr>
      <tr><td>LLM confidence · SELL</td><td>${conf("SELL")}</td></tr>
    </table></div>
    <div><h3>Return since signal (marked at last price)</h3><table>
      <tr><td>LLM after BUY</td><td>${f("llm", "BUY")}</td></tr>
      <tr><td>Rules after BUY</td><td>${f("rules", "BUY")}</td></tr>
      <tr><td>LLM after SELL</td><td>${f("llm", "SELL")}</td></tr>
      <tr><td>Rules after SELL</td><td>${f("rules", "SELL")}</td></tr>
    </table></div>
    <div><h3>Operations &amp; income</h3><table>
      <tr><td>LLM latency avg / max</td><td>${R.latency_avg_s ?? "—"}s / ${R.latency_max_s ?? "—"}s</td></tr>
      <tr><td>Trades executed (LLM / Rules)</td><td>${R.exec_trades.llm || 0} / ${R.exec_trades.rules || 0}</td></tr>
      <tr><td>Div + interest · LLM</td><td>${inc("llm")}</td></tr>
      <tr><td>Div + interest · Rules</td><td>${inc("rules")}</td></tr>
      <tr><td>Div + interest · SPY B&amp;H</td><td>${inc("benchmark")}</td></tr>
    </table></div>`;
})();

/* ---- signal mix ---- */
document.getElementById("mix").innerHTML =
  "<tr><th>Arm</th><th>BUY</th><th>HOLD</th><th>SELL</th></tr>" +
  ARMS.filter(a => a.id !== "benchmark").map(a => {
    const c = D.counts[a.id] || {};
    return `<tr><td><span class="dot" style="background:${a.color}"></span> ${a.label}</td>
      <td class="num">${c.BUY || 0}</td><td class="num">${c.HOLD || 0}</td><td class="num">${c.SELL || 0}</td></tr>`;
  }).join("");

/* ---- decisions table ---- */
document.getElementById("dec").innerHTML =
  "<tr><th>When (UTC)</th><th>Arm</th><th>Ticker</th><th>Signal</th><th>Price</th><th>Reasoning</th></tr>" +
  (D.decisions.length ? D.decisions.map(d => `
    <tr><td>${d.created_utc.replace("T", " ").slice(0, 16)}</td>
        <td class="armtag">${d.arm}</td><td><b>${d.ticker}</b></td>
        <td><span class="sig ${d.signal}">${d.signal}</span></td>
        <td class="num">${d.price ? "$$" + d.price.toFixed(2) : "—"}</td>
        <td class="rationale">${(d.rationale || "").replace(/</g, "&lt;")}
            ${d.error ? `<div class="err">error: ${String(d.error).replace(/</g, "&lt;")}</div>` : ""}</td></tr>`).join("")
   : '<tr><td colspan="6" class="empty">No decisions yet.</td></tr>');

/* ---- run history ---- */
document.getElementById("runs").innerHTML =
  "<tr><th>#</th><th>Started (UTC)</th><th>Status</th><th>Model</th><th>Notes</th></tr>" +
  (D.runs.length ? D.runs.map(r => `
    <tr><td class="num">${r.run_id}</td><td>${r.started_utc.replace("T", " ").slice(0, 16)}</td>
        <td>${r.status}</td><td class="armtag">${r.llm_model || ""}</td><td class="armtag">${r.notes || ""}</td></tr>`).join("")
   : '<tr><td colspan="5" class="empty">No runs yet.</td></tr>');
</script>
</body>
</html>
""")


def generate() -> str:
    data = collect()
    # safe_substitute: only $DATA/$MODEL/$GENERATED are ours; the template's
    # JavaScript ${...} literals must pass through untouched.
    html = TEMPLATE.safe_substitute(
        DATA=json.dumps(data),
        MODEL=data["model"],
        GENERATED=data["generated"],
    )
    OUT_PATH.write_text(html)
    return str(OUT_PATH)


if __name__ == "__main__":
    print("Dashboard written to", generate())
