#!/usr/bin/env python3
"""Gera o relatório (docs/index.html) a partir de passagens.db.

Uso:
  python build_dashboard.py
  python build_dashboard.py --db outro.db --out saida.html
"""
import argparse
import json
import os
import sqlite3
import statistics
from datetime import date, datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "config.json")))

# Parâmetros dos sinais (simples e explícitos; ganham confiança com mais coletas)
MIN_OBS = 3            # coletas mínimas por viagem para emitir sinal
AT_BEST_TOL = 1.02     # "no menor preço visto" = até 2% acima do mínimo histórico
FAR_ABOVE = 1.05       # "acima do menor visto" = mais de 5% acima
SCARCE_SHARE = 0.25    # menos de 25% dos voos no piso = tarifa baixa escassa
FINAL_WINDOW_DAYS = 28
BIN = 14               # largura (dias) das faixas de antecedência


def iso_days(a, b):
    return (date.fromisoformat(a) - date.fromisoformat(b)).days


def load(db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT s.id, s.collected_at, s.origin, s.destination, s.outbound_date, s.return_date,
                  s.min_price, s.n_offers,
                  (SELECT COUNT(*) FROM offers o WHERE o.search_id = s.id AND o.price = s.min_price) AS n_floor
           FROM searches s
           WHERE s.status = 'ok' AND s.min_price IS NOT NULL
           ORDER BY s.collected_at, s.id"""
    ).fetchall()

    month = datetime.now(timezone.utc).strftime("%Y-%m")
    used = con.execute(
        "SELECT COUNT(*) FROM searches WHERE provider='serpapi' AND status!='error' "
        "AND substr(collected_at,1,7)=?", (month,)).fetchone()[0]

    if not rows:
        return None, con, used

    trips = {}
    for r in rows:
        key = (r["origin"], r["destination"], r["outbound_date"])
        t = trips.setdefault(key, {"o": key[0], "d": key[1], "out": key[2],
                                   "ret": r["return_date"], "by_day": {}})
        day = r["collected_at"][:10]
        t["by_day"][day] = {  # a última coleta do dia vence
            "d": day, "p": r["min_price"],
            "s": round(r["n_floor"] / r["n_offers"], 3) if r["n_offers"] else None,
            "sid": r["id"],
        }
    asof = max(r["collected_at"][:10] for r in rows)
    return {"trips": trips, "asof": asof, "first": min(r["collected_at"][:10] for r in rows)}, con, used


def evaluate(t, asof):
    obs = sorted(t["by_day"].values(), key=lambda x: x["d"])
    for ob in obs:
        ob["dtd"] = iso_days(t["out"], ob["d"])
    t["obs"] = obs
    n = len(obs)
    cur = obs[-1]
    prices = [o["p"] for o in obs]
    best, worst = min(prices), max(prices)
    prev7 = [o for o in obs if iso_days(asof, o["d"]) >= 7]
    chg7 = (cur["p"] / prev7[-1]["p"] - 1) if prev7 else None
    share = cur["s"]
    dtd = iso_days(t["out"], asof)

    sig, why = "coletando", f"{n} de {MIN_OBS} coletas mínimas para avaliar esta viagem."
    if n >= MIN_OBS:
        at_best = cur["p"] <= best * AT_BEST_TOL
        share_drop = False
        if prev7 and prev7[-1]["s"] and share is not None:
            share_drop = share < prev7[-1]["s"] * 0.7
        scarce = (share is not None and share < SCARCE_SHARE) or share_drop
        if at_best and (scarce or dtd <= FINAL_WINDOW_DAYS):
            sig = "comprar"
            motivo = ("as tarifas mais baixas estão acabando" if scarce
                      else f"faltam {dtd} dias para o embarque")
            why = f"Está no menor preço já visto e {motivo}."
        elif cur["p"] > best * FAR_ABOVE and chg7 is not None and chg7 < 0:
            sig = "aguardar"
            why = f"Está {round((cur['p'] / best - 1) * 100)}% acima do menor preço visto e em queda."
        elif at_best:
            sig = "acompanhar"
            why = "No menor preço já visto e ainda há tarifas baixas; sem pressa."
        else:
            sig = "acompanhar"
            sub = " e subindo" if (chg7 is not None and chg7 > 0.03) else ""
            why = f"Está {round((cur['p'] / best - 1) * 100)}% acima do menor preço visto{sub}."
    return {
        "id": f"{t['o']}>{t['d']}|{t['out']}", "o": t["o"], "d": t["d"], "out": t["out"], "ret": t["ret"],
        "obs": [{"d": o["d"], "p": o["p"], "s": o["s"], "dtd": o["dtd"]} for o in obs],
        "cur": cur["p"], "best": best, "worst": worst, "share": share, "dtd": dtd,
        "chg7": chg7, "signal": sig, "reason": why, "sid": cur["sid"],
    }


def window_curve(all_trips):
    pts, ready = [], 0
    for t in all_trips:
        if len(t["obs"]) >= MIN_OBS:
            ready += 1
            b = t["best"]
            pts += [(o["dtd"], o["p"] / b, o["p"]) for o in t["obs"]]
    buckets = {}
    for dtd, idx, p in pts:
        buckets.setdefault((dtd // BIN) * BIN, []).append((idx, p))
    out = []
    for b in sorted(buckets, reverse=True):
        v = buckets[b]
        if len(v) >= 3:
            out.append({"label": f"{b}–{b + BIN - 1}", "lo": b, "n": len(v),
                        "idx": round(statistics.median(i for i, _ in v), 4),
                        "price": round(statistics.median(p for _, p in v))})
    text = None
    if ready >= 4 and len(pts) >= 30 and out:
        solid = [b for b in out if b["n"] >= 5] or out
        low = min(b["idx"] for b in solid)
        floor_lo = min(b["lo"] for b in solid if b["idx"] <= low + 0.01)
        closer = [b["idx"] for b in solid if b["lo"] < floor_lo]
        if not closer:
            text = "O preço se manteve no piso em todas as faixas de antecedência observadas até agora."
        else:
            rise = round((max(closer) / low - 1) * 100)
            text = (f"O preço fica no piso quando faltam {floor_lo} dias ou mais; "
                    f"nas faixas mais próximas do embarque o preço relativo chega a +{rise}%.")
    return {"buckets": out, "ready": ready, "points": len(pts), "text": text}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(HERE, CFG["db_path"]))
    ap.add_argument("--out", default=os.path.join(HERE, "docs", "index.html"))
    args = ap.parse_args()

    data, con, used = load(args.db)
    payload = {"empty": True}
    if data:
        asof = data["asof"]
        evaluated = [evaluate(t, asof) for t in data["trips"].values()]
        upcoming = sorted([t for t in evaluated if t["out"] >= asof], key=lambda t: (t["out"], t["o"]))
        for t in upcoming:
            t["flights"] = [
                {"p": r[0], "a": r[1], "f": r[2], "dep": (r[3] or "")[-5:], "arr": (r[4] or "")[-5:], "st": r[5]}
                for r in con.execute(
                    "SELECT price, airline, flight_number, dep_time, arr_time, stops FROM offers "
                    "WHERE search_id=? ORDER BY price, dep_time LIMIT 6", (t["sid"],))]
        payload = {
            "empty": False, "asof": asof, "first": data["first"],
            "runs": len({d for t in data["trips"].values() for d in t["by_day"]}),
            "budget": {"used": used, "limit": CFG.get("monthly_search_budget")},
            "trips": upcoming, "window": window_curve(evaluated),
            "min_obs": MIN_OBS,
        }
    html = TEMPLATE.replace("__DATA__", json.dumps(payload, ensure_ascii=False).replace("</", "<\\/"))
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Relatório gerado: {args.out} ({len(payload.get('trips', []))} viagens futuras)")


TEMPLATE = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Passagens SDU↔CGH</title>
<style>
:root{--surface:#fcfcfb;--page:#f9f9f7;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--grid:#e1e0d9;--axis:#c3c2b7;
--border:rgba(11,11,11,.10);--s1:#2a78d6;--s2:#eb6834;--good:#0ca30c;--warn:#fab219;--wash:rgba(11,11,11,.04);--onwarn:#0b0b0b}
@media (prefers-color-scheme:dark){:root{--surface:#1a1a19;--page:#0d0d0d;--ink:#f0efec;--ink2:#c3c2b7;--grid:#2c2c2a;
--axis:#383835;--border:rgba(255,255,255,.10);--s1:#3987e5;--s2:#d95926;--wash:rgba(255,255,255,.05)}}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1040px;margin:0 auto;padding:20px 16px 48px}
h1{font-size:22px;margin:0 0 2px}
h2{font-size:17px;margin:0 0 4px}
.sub{color:var(--ink2);margin:0}
.muted{color:var(--muted)}
section{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:18px;margin-top:16px}
.hero{font-size:20px;font-weight:600;margin:6px 0 10px;line-height:1.35}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}
button{font:inherit;color:inherit}
.chip{border:1px solid var(--border);background:transparent;border-radius:999px;padding:5px 12px;cursor:pointer}
.chip:hover{background:var(--wash)}
.chip[aria-pressed=true]{background:var(--ink);color:var(--surface);border-color:var(--ink)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-top:12px}
.tile{border:1px solid var(--border);border-radius:10px;padding:10px 12px}
.tile b{display:block;font-size:26px;line-height:1.1}
.tile span{color:var(--ink2);font-size:13px}
.banner{background:var(--wash);border-radius:10px;padding:10px 12px;color:var(--ink2);margin-top:10px;font-size:14px}
.legend{display:flex;gap:16px;flex-wrap:wrap;margin:6px 0 2px;color:var(--ink2);font-size:13px}
.legend i{display:inline-block;width:12px;height:3px;border-radius:2px;margin-right:6px;vertical-align:middle}
svg{width:100%;height:auto;display:block;overflow:visible}
svg text{font:11px system-ui,sans-serif;fill:var(--muted)}
.tbl{width:100%;border-collapse:collapse;margin-top:8px;font-size:14px}
.tbl th{text-align:left;font-weight:600;color:var(--ink2);font-size:12px;padding:6px 8px;border-bottom:1px solid var(--axis);white-space:nowrap}
.tbl td{padding:8px;border-bottom:1px solid var(--grid);white-space:nowrap}
.tbl td.n,.tbl th.n{text-align:right;font-variant-numeric:tabular-nums}
.tbl tbody tr{cursor:pointer}
.tbl tbody tr:hover{background:var(--wash)}
.tbl tbody tr[aria-selected=true]{background:var(--wash);box-shadow:inset 3px 0 0 var(--ink)}
.scroll{overflow-x:auto}
.pill{display:inline-flex;align-items:center;gap:7px;font-size:13px}
.pill i{width:18px;height:18px;border-radius:50%;display:inline-grid;place-items:center;font-style:normal;font-size:11px;font-weight:700;color:#fff}
.pill.good i{background:var(--good)}
.pill.warn i{background:var(--warn);color:var(--onwarn)}
.pill.mute i{background:var(--muted)}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:760px){.grid2{grid-template-columns:1fr}.hero{font-size:18px}}
.fl{width:100%;border-collapse:collapse;font-size:13px;margin-top:6px}
.fl td,.fl th{padding:5px 6px;border-bottom:1px solid var(--grid);text-align:left}
.fl th{color:var(--ink2);font-weight:600}
details{margin-top:12px;color:var(--ink2);font-size:13px}
summary{cursor:pointer}
footer{color:var(--muted);font-size:12px;margin-top:14px}
#tip{position:fixed;pointer-events:none;background:var(--surface);color:var(--ink);border:1px solid var(--axis);border-radius:8px;
padding:6px 9px;font-size:12px;box-shadow:0 4px 14px rgba(0,0,0,.15);display:none;z-index:9;max-width:240px}
#tip div{display:flex;align-items:center;gap:6px}
</style>
</head>
<body>
<main>
<h1>Passagens SDU ↔ CGH</h1>
<p class="sub" id="meta"></p>
<div id="app"></div>
<footer id="foot"></footer>
</main>
<div id="tip"></div>
<script>
const D = __DATA__;
const $ = s => document.querySelector(s);
const brl = n => n == null ? '–' : 'R$ ' + Math.round(n).toLocaleString('pt-BR');
const WD = ['dom','seg','ter','qua','qui','sex','sáb'];
const dt = iso => new Date(iso + 'T12:00:00');
const dm = iso => { const d = dt(iso); return String(d.getDate()).padStart(2,'0') + '/' + String(d.getMonth()+1).padStart(2,'0'); };
const dwm = iso => WD[dt(iso).getDay()] + ' ' + dm(iso);
const rt = t => t.o + '→' + t.d;
const SIG = {
  comprar:    {label:'Comprar agora',   icon:'✓', cls:'good'},
  aguardar:   {label:'Aguardar queda',  icon:'↓', cls:'warn'},
  acompanhar: {label:'Acompanhar',      icon:'•', cls:'mute'},
  coletando:  {label:'Coletando dados', icon:'…', cls:'mute'}
};
const pill = s => `<span class="pill ${SIG[s].cls}"><i>${SIG[s].icon}</i>${SIG[s].label}</span>`;
const SERIES = {'SDU>CGH': {name:'SDU→CGH', color:'var(--s1)'}, 'CGH>SDU': {name:'CGH→SDU', color:'var(--s2)'}};
const tip = $('#tip');

function niceTicks(lo, hi, n) {
  const raw = (hi - lo) / n, p = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map(m => m * p).find(s => s >= raw);
  const a = Math.floor(lo / step) * step, b = Math.ceil(hi / step) * step, t = [];
  for (let v = a; v <= b + 1e-9; v += step) t.push(v);
  return t;
}

// Gráfico de linhas SVG: cats (rótulos), series [{name,color,vals}], tooltip e legenda automáticos
function lineChart(host, o) {
  const W = o.w || 760, H = o.h || 240, m = {l: 56, r: 14, t: 12, b: 28};
  const all = o.series.flatMap(s => s.vals.filter(v => v != null));
  let lo = Math.min(...all), hi = Math.max(...all);
  if (lo === hi) { lo *= 0.92; hi *= 1.08; }
  const pad = (hi - lo) * 0.15; lo = Math.max(0, lo - pad); hi += pad;
  const ticks = niceTicks(lo, hi, 4); lo = ticks[0]; hi = ticks[ticks.length - 1];
  const n = o.cats.length, iw = W - m.l - m.r - 28;
  const X = i => n === 1 ? m.l + (W - m.l - m.r) / 2 : m.l + 14 + i * iw / (n - 1);
  const Y = v => m.t + (H - m.t - m.b) * (1 - (v - lo) / (hi - lo));
  let s = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${o.aria || ''}">`;
  ticks.forEach(v => {
    s += `<line x1="${m.l}" x2="${W - m.r}" y1="${Y(v)}" y2="${Y(v)}" style="stroke:var(--grid)" stroke-width="1"/>`;
    s += `<text x="${m.l - 8}" y="${Y(v) + 4}" text-anchor="end">${o.yfmt ? o.yfmt(v) : brl(v)}</text>`;
  });
  s += `<line x1="${m.l}" x2="${W - m.r}" y1="${H - m.b}" y2="${H - m.b}" style="stroke:var(--axis)"/>`;
  const every = Math.ceil(n / (W < 500 ? 4 : 8));
  o.cats.forEach((c, i) => { if (i % every === 0 || i === n - 1) s += `<text x="${X(i)}" y="${H - 8}" text-anchor="middle">${c}</text>`; });
  o.series.forEach(se => {
    let d = '', pen = false;
    se.vals.forEach((v, i) => { if (v == null) { pen = false; return; } d += (pen ? 'L' : 'M') + X(i) + ' ' + Y(v); pen = true; });
    s += `<path d="${d}" fill="none" style="stroke:${se.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
    se.vals.forEach((v, i) => { if (v != null) s += `<circle cx="${X(i)}" cy="${Y(v)}" r="4" style="fill:${se.color};stroke:var(--surface)" stroke-width="2"/>`; });
  });
  s += `<line id="xh" y1="${m.t}" y2="${H - m.b}" style="stroke:var(--axis);display:none"/>`;
  s += `<rect x="0" y="0" width="${W}" height="${H}" fill="transparent" class="hit"/></svg>`;
  let leg = '';
  if (o.series.length > 1) leg = '<div class="legend">' + o.series.map(se => `<span><i style="background:${se.color}"></i>${se.name}</span>`).join('') + '</div>';
  host.innerHTML = leg + s;
  const svg = host.querySelector('svg'), xh = svg.querySelector('#xh');
  const move = ev => {
    const r = svg.getBoundingClientRect(), px = (ev.clientX - r.left) * W / r.width;
    let i = Math.round((px - m.l - 14) / (iw / Math.max(1, n - 1))); i = Math.max(0, Math.min(n - 1, i));
    xh.setAttribute('x1', X(i)); xh.setAttribute('x2', X(i)); xh.style.display = '';
    let h = `<b>${o.cats[i]}</b>`;
    o.series.forEach(se => { if (se.vals[i] != null) h += `<div><span class="dot" style="background:${se.color};margin:0"></span>${o.series.length > 1 ? se.name + ': ' : ''}${o.yfmt ? o.yfmt(se.vals[i]) : brl(se.vals[i])}</div>`; });
    if (o.extra && o.extra[i]) h += `<div class="muted">${o.extra[i]}</div>`;
    tip.innerHTML = h; tip.style.display = 'block';
    tip.style.left = Math.min(ev.clientX + 14, innerWidth - 250) + 'px'; tip.style.top = (ev.clientY + 14) + 'px';
  };
  svg.addEventListener('pointermove', move); svg.addEventListener('pointerdown', move);
  svg.addEventListener('pointerleave', () => { tip.style.display = 'none'; xh.style.display = 'none'; });
}

function render() {
  if (D.empty) { $('#app').innerHTML = '<section><h2>Sem dados ainda</h2><p class="sub">Rode a primeira coleta para o relatório aparecer.</p></section>'; return; }
  $('#meta').textContent = `Atualizado em ${dm(D.asof)} · ${D.runs} ${D.runs === 1 ? 'dia' : 'dias'} de coleta desde ${dm(D.first)}`;
  const T = D.trips, by = k => T.filter(t => t.signal === k);
  const buy = by('comprar'), wait = by('aguardar'), coll = by('coletando'), watch = by('acompanhar');
  let sel = (buy[0] || T[0] || {}).id, filt = 'all';

  const app = $('#app');
  app.innerHTML = `
  <section id="s1"></section>
  <section id="s2"><h2>Preço mínimo por viagem (ida e volta)</h2>
    <p class="sub">Menor tarifa encontrada hoje para cada quinta monitorada.</p><div id="c2"></div></section>
  <section id="s3"><h2>Decisão por viagem</h2>
    <div class="chips" id="flt"></div><div class="scroll" id="tb"></div></section>
  <section id="s4"></section>
  <section id="s5"></section>`;

  // 1. Veredito
  let h = '<h2>Qual é o melhor momento?</h2>';
  if (buy.length) {
    h += `<p class="hero">Comprar agora: ${buy.length} ${buy.length === 1 ? 'viagem' : 'viagens'}</p><div class="chips">` +
      buy.map(t => `<button class="chip" data-go="${t.id}">${dwm(t.out)} · ${rt(t)} · ${brl(t.cur)}</button>`).join('') + '</div>';
  } else if (coll.length === T.length) {
    h += `<p class="hero">Ainda coletando histórico</p>`;
  } else {
    h += `<p class="hero">Nenhuma viagem pede compra imediata</p>`;
  }
  h += `<div class="tiles">
    <div class="tile"><b>${buy.length}</b><span>comprar agora</span></div>
    <div class="tile"><b>${wait.length}</b><span>aguardar queda</span></div>
    <div class="tile"><b>${watch.length}</b><span>acompanhar</span></div>
    <div class="tile"><b>${coll.length}</b><span>coletando dados</span></div></div>`;
  if (coll.length) h += `<div class="banner">O sinal de cada viagem só é emitido a partir da ${D.min_obs}ª coleta. Com coletas às segundas e quintas, as primeiras recomendações aparecem em cerca de 8 a 11 dias.</div>`;
  $('#s1').innerHTML = h;

  // 2. Mapa de preços
  const dates = [...new Set(T.map(t => t.out))].sort();
  const ser = Object.keys(SERIES).map(k => ({name: SERIES[k].name, color: SERIES[k].color,
    vals: dates.map(d => { const t = T.find(x => x.out === d && x.o + '>' + x.d === k); return t ? t.cur : null; })}));
  lineChart($('#c2'), {cats: dates.map(dwm), series: ser, aria: 'Preço mínimo por quinta e rota'});

  // 3. Tabela
  const fl = $('#flt');
  fl.innerHTML = [['all', 'Todas'], ['SDU>CGH', 'SDU→CGH'], ['CGH>SDU', 'CGH→SDU']]
    .map(([k, l]) => `<button class="chip" data-f="${k}" aria-pressed="${k === filt}">${l}</button>`).join('');
  fl.onclick = e => { const b = e.target.closest('[data-f]'); if (!b) return; filt = b.dataset.f; fl.querySelectorAll('.chip').forEach(c => c.setAttribute('aria-pressed', c.dataset.f === filt)); table(); };
  function table() {
    const rows = T.filter(t => filt === 'all' || t.o + '>' + t.d === filt);
    $('#tb').innerHTML = `<table class="tbl"><thead><tr><th>Ida</th><th>Rota</th><th class="n">Dias</th><th class="n">Hoje</th><th class="n">Menor visto</th><th class="n">Voos no piso</th><th>Sinal</th></tr></thead><tbody>` +
      rows.map(t => `<tr data-id="${t.id}" tabindex="0" aria-selected="${t.id === sel}"><td>${dwm(t.out)}</td><td><span class="dot" style="background:${SERIES[t.o + '>' + t.d].color}"></span>${rt(t)}</td>
      <td class="n">${t.dtd}</td><td class="n">${brl(t.cur)}</td><td class="n">${brl(t.best)}</td><td class="n">${t.share == null ? '–' : Math.round(t.share * 100) + '%'}</td><td>${pill(t.signal)}</td></tr>`).join('') + '</tbody></table>';
  }
  table();
  $('#tb').onclick = e => { const r = e.target.closest('tr[data-id]'); if (r) pick(r.dataset.id); };
  $('#tb').onkeydown = e => { if (e.key === 'Enter') { const r = e.target.closest('tr[data-id]'); if (r) pick(r.dataset.id); } };
  $('#s1').onclick = e => { const b = e.target.closest('[data-go]'); if (b) { pick(b.dataset.go); $('#s4').scrollIntoView({behavior: 'smooth'}); } };

  // 4. Detalhe da viagem
  function pick(id) { sel = id; table(); drill(); }
  function drill() {
    const t = T.find(x => x.id === sel); if (!t) { $('#s4').innerHTML = ''; return; }
    $('#s4').innerHTML = `<h2>${rt(t)} · ${dwm(t.out)} → ${dwm(t.ret)}</h2>
      <p class="sub">${pill(t.signal)} &nbsp;${t.reason}</p>
      <div class="grid2" style="margin-top:12px"><div><b style="font-size:13px">Preço mínimo ao longo das coletas</b><div id="d1"></div></div>
      <div><b style="font-size:13px">Voos ainda no preço mínimo</b><div id="d2"></div></div></div>
      <b style="font-size:13px;display:block;margin-top:14px">Voos mais baratos hoje</b>
      <table class="fl"><thead><tr><th>Preço</th><th>Companhia</th><th>Voo</th><th>Saída</th><th>Chegada</th></tr></thead><tbody>` +
      (t.flights || []).map(f => `<tr><td>${brl(f.p)}</td><td>${f.a}</td><td>${f.f || ''}</td><td>${f.dep}</td><td>${f.arr}</td></tr>`).join('') + '</tbody></table>';
    const cats = t.obs.map(o => dm(o.d));
    lineChart($('#d1'), {cats, w: 380, h: 220, series: [{name: 'Preço mínimo', color: 'var(--s1)', vals: t.obs.map(o => o.p)}],
      extra: t.obs.map(o => o.dtd + ' dias para o embarque'), aria: 'Preço mínimo por dia de coleta'});
    lineChart($('#d2'), {cats, w: 380, h: 220, yfmt: v => Math.round(v) + '%', series: [{name: 'Voos no piso', color: 'var(--s1)', vals: t.obs.map(o => o.s == null ? null : o.s * 100)}],
      aria: 'Percentual de voos no preço mínimo'});
  }
  drill();

  // 5. Janela de compra
  const w = D.window;
  let wh = '<h2>Qual a janela de compra?</h2>';
  if (w.buckets.length && w.text) {
    wh += `<p class="hero" style="font-size:17px">${w.text}</p><p class="sub">Preço relativo ao menor valor de cada viagem, por dias de antecedência (1,00 = menor preço da viagem).</p><div id="c5"></div>`;
  } else {
    wh += `<p class="sub">Ainda sem histórico suficiente para responder. Precisamos de pelo menos 4 viagens com ${D.min_obs} coletas cada e 30 observações (hoje: ${w.ready} viagens, ${w.points} observações).</p>`;
  }
  $('#s5').innerHTML = wh + `<details><summary>Como os sinais são definidos</summary>
    <p><b>Comprar agora:</b> preço atual até 2% do menor já visto e (menos de 25% dos voos no piso, ou queda de 30% nessa fatia em 7 dias, ou faltam 28 dias ou menos).<br>
    <b>Aguardar queda:</b> mais de 5% acima do menor visto e caindo na última semana.<br>
    <b>Acompanhar:</b> demais casos. <b>Coletando dados:</b> menos de ${D.min_obs} coletas.<br>
    Regras simples e indicativas: ganham confiança conforme o histórico cresce.</p></details>`;
  if (w.buckets.length && w.text) lineChart($('#c5'), {cats: w.buckets.map(b => b.label + ' dias'), h: 220, yfmt: v => v.toFixed(2).replace('.', ','),
    series: [{name: 'Preço relativo', color: 'var(--s1)', vals: w.buckets.map(b => b.idx)}],
    extra: w.buckets.map(b => b.n + ' observações · mediana ' + brl(b.price)), aria: 'Preço relativo por antecedência'});

  const b = D.budget;
  $('#foot').textContent = b.limit ? `Buscas usadas neste mês: ${b.used} de ${b.limit} (limite com folga de 5% do plano gratuito).` : '';
}
render();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
