#!/usr/bin/env python3
"""Coletor de passagens SDU<->CGH (ida e volta), quinta -> segunda.

Uso:
  python collect.py                       # coleta com o provedor do config.json
  python collect.py --provider mock       # teste sem API
  python collect.py --plan                # só mostra as viagens planejadas
  python collect.py --export csv          # exporta tabela plana para CSV
"""
import argparse
import csv
import json
import os
import random
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

CFG = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")))

SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collected_at TEXT NOT NULL,          -- UTC ISO
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    outbound_date TEXT NOT NULL,
    return_date TEXT NOT NULL,
    days_to_departure INTEGER NOT NULL,
    provider TEXT NOT NULL,
    status TEXT NOT NULL,                -- ok | empty | error
    error TEXT,
    n_offers INTEGER DEFAULT 0,
    min_price REAL,
    currency TEXT,
    insight_level TEXT,                  -- low | typical | high (quando o provedor informa)
    typical_low REAL,
    typical_high REAL
);

CREATE TABLE IF NOT EXISTS offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    search_id INTEGER NOT NULL REFERENCES searches(id),
    rank INTEGER,
    price REAL,                          -- total ida e volta (por passageiro, conforme o provedor)
    airline TEXT,
    flight_number TEXT,
    stops INTEGER,
    dep_time TEXT,
    arr_time TEXT,
    duration_min INTEGER
);

CREATE INDEX IF NOT EXISTS ix_searches_trip ON searches(origin, destination, outbound_date, collected_at);

-- Menor preço por dia de coleta e viagem: base para correlacionar custo x antecedência
CREATE VIEW IF NOT EXISTS v_daily_min AS
SELECT date(collected_at) AS collected_day,
       origin, destination, outbound_date, return_date,
       CAST(strftime('%w', outbound_date) AS INTEGER) AS outbound_dow,
       days_to_departure,
       MIN(min_price) AS min_price
FROM searches
WHERE status = 'ok'
GROUP BY 1, 2, 3, 4, 5, 7;
"""


# ----------------------------------------------------------------- viagens
def planned_trips(today=None):
    """Quintas futuras (ida) + segunda seguinte (volta). Rota alterna a cada semana."""
    today = today or date.today()
    weekday_idx = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                   "friday": 4, "saturday": 5, "sunday": 6}[CFG["outbound_weekday"]]
    anchor = date.fromisoformat(CFG["anchor_thursday"])
    route_a, route_b = tuple(CFG["route_a"]), tuple(CFG["route_b"])
    first_is_a = CFG["anchor_route"].lower() == "a"

    # primeira ida: start_date (se definida) ou a próxima quinta a partir de amanhã
    d = date.fromisoformat(CFG["start_date"]) if CFG.get("start_date") else today + timedelta(days=1)
    d = max(d, today + timedelta(days=1))
    while d.weekday() != weekday_idx:
        d += timedelta(days=1)

    trips = []
    for k in range(CFG["horizon_weeks"]):
        out = d + timedelta(weeks=k)
        ret = out + timedelta(days=CFG["return_offset_days"])
        weeks_from_anchor = (out - anchor).days // 7
        is_a = (weeks_from_anchor % 2 == 0) == first_is_a
        main, other = (route_a, route_b) if is_a else (route_b, route_a)
        trips.append((main[0], main[1], out, ret))
        if CFG["collect_mode"] == "both":
            trips.append((other[0], other[1], out, ret))
    return trips


# ---------------------------------------------------------------- provedores
def fetch_serpapi(origin, dest, out, ret):
    key = os.environ.get("SERPAPI_KEY")
    if not key:
        raise RuntimeError("Defina a variável de ambiente SERPAPI_KEY")
    params = {
        "engine": "google_flights", "departure_id": origin, "arrival_id": dest,
        "outbound_date": out.isoformat(), "return_date": ret.isoformat(),
        "type": 1, "adults": CFG["adults"], "currency": CFG["currency"],
        "hl": "pt", "gl": "br", "api_key": key,
    }
    url = "https://serpapi.com/search.json?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=60) as r:
        data = json.load(r)
    if "error" in data and not (data.get("best_flights") or data.get("other_flights")):
        if "hasn't returned any results" in data["error"]:
            return [], {}
        raise RuntimeError(data["error"])

    offers = []
    for item in (data.get("best_flights") or []) + (data.get("other_flights") or []):
        legs = item.get("flights") or []
        if not legs or item.get("price") is None:
            continue
        offers.append({
            "price": item["price"],
            "airline": legs[0].get("airline"),
            "flight_number": legs[0].get("flight_number"),
            "stops": len(legs) - 1,
            "dep_time": (legs[0].get("departure_airport") or {}).get("time"),
            "arr_time": (legs[-1].get("arrival_airport") or {}).get("time"),
            "duration_min": item.get("total_duration"),
        })
    pi = data.get("price_insights") or {}
    rng = pi.get("typical_price_range") or [None, None]
    insight = {"level": pi.get("price_level"), "low": rng[0], "high": rng[-1]}
    return offers, insight


def fetch_mock(origin, dest, out, ret):
    """Dados sintéticos só para testar o pipeline."""
    days = (out - date.today()).days
    base = 550 + max(0, 30 - days) * 12
    offers = []
    for i, airline in enumerate(["LATAM", "GOL", "Azul"]):
        offers.append({
            "price": round(base * random.uniform(0.85, 1.25) + i * 20, 2),
            "airline": airline, "flight_number": f"{airline[:2].upper()} {1000 + random.randint(0, 999)}",
            "stops": 0, "dep_time": f"{out} {7 + i * 4:02d}:00", "arr_time": f"{out} {8 + i * 4:02d}:05",
            "duration_min": 65,
        })
    return offers, {"level": "typical", "low": round(base * 0.8), "high": round(base * 1.2)}


PROVIDERS = {"serpapi": fetch_serpapi, "mock": fetch_mock}


# --------------------------------------------------------------------- banco
def connect():
    path = CFG["db_path"]
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con


def searches_used_this_month(con, provider):
    """Consultas pagas já feitas no mês corrente (UTC) para o provedor."""
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    return con.execute(
        "SELECT COUNT(*) FROM searches WHERE provider = ? AND status != 'error' "
        "AND substr(collected_at, 1, 7) = ?", (provider, month)).fetchone()[0]


def save(con, provider, origin, dest, out, ret, offers, insight, error=None):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    status = "error" if error else ("ok" if offers else "empty")
    prices = [o["price"] for o in offers]
    cur = con.execute(
        """INSERT INTO searches (collected_at, origin, destination, outbound_date, return_date,
           days_to_departure, provider, status, error, n_offers, min_price, currency,
           insight_level, typical_low, typical_high) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (now, origin, dest, out.isoformat(), ret.isoformat(), (out - date.today()).days,
         provider, status, error, len(offers), min(prices) if prices else None, CFG["currency"],
         (insight or {}).get("level"), (insight or {}).get("low"), (insight or {}).get("high")),
    )
    sid = cur.lastrowid
    for rank, o in enumerate(sorted(offers, key=lambda x: x["price"]), start=1):
        con.execute(
            """INSERT INTO offers (search_id, rank, price, airline, flight_number, stops,
               dep_time, arr_time, duration_min) VALUES (?,?,?,?,?,?,?,?,?)""",
            (sid, rank, o["price"], o["airline"], o["flight_number"], o["stops"],
             o["dep_time"], o["arr_time"], o["duration_min"]),
        )
    con.commit()


def export_csv(con, path="passagens_flat.csv"):
    rows = con.execute(
        """SELECT s.collected_at, s.origin, s.destination, s.outbound_date, s.return_date,
                  s.days_to_departure, s.insight_level, s.typical_low, s.typical_high,
                  o.rank, o.price, o.airline, o.flight_number, o.stops, o.dep_time, o.arr_time, o.duration_min
           FROM searches s JOIN offers o ON o.search_id = s.id ORDER BY s.collected_at, s.id, o.rank"""
    ).fetchall()
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["collected_at", "origin", "destination", "outbound_date", "return_date",
                    "days_to_departure", "insight_level", "typical_low", "typical_high",
                    "rank", "price", "airline", "flight_number", "stops", "dep_time", "arr_time", "duration_min"])
        w.writerows(rows)
    print(f"{len(rows)} linhas exportadas para {path}")


# ---------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default=CFG["provider"], choices=PROVIDERS)
    ap.add_argument("--horizon", type=int, help="sobrescreve horizon_weeks")
    ap.add_argument("--plan", action="store_true", help="apenas lista as viagens planejadas")
    ap.add_argument("--export", choices=["csv"])
    args = ap.parse_args()
    if args.horizon:
        CFG["horizon_weeks"] = args.horizon

    if args.export:
        export_csv(connect())
        return

    trips = planned_trips()
    if args.plan:
        for o, d, out, ret in trips:
            print(f"{o}->{d}  ida {out:%a %d/%m/%Y}  volta {ret:%a %d/%m/%Y}")
        print(f"{len(trips)} buscas por rodada")
        return

    con = connect()
    fetch = PROVIDERS[args.provider]
    ok = fail = 0
    budget = CFG.get("monthly_search_budget") if args.provider == "serpapi" else None
    used = searches_used_this_month(con, args.provider) if budget else 0
    if budget:
        print(f"Orçamento do mês: {used}/{budget} consultas usadas")
    for origin, dest, out, ret in trips:
        if budget and used >= budget:
            print(f"Orçamento mensal atingido ({budget}); demais buscas ficam para o próximo mês.")
            break
        used += 1
        try:
            offers, insight = fetch(origin, dest, out, ret)
            save(con, args.provider, origin, dest, out, ret, offers, insight)
            best = min((o["price"] for o in offers), default=None)
            print(f"[ok]   {origin}->{dest} {out} / {ret}: {len(offers)} ofertas, menor {best}")
            ok += 1
        except Exception as e:  # noqa: BLE001
            save(con, args.provider, origin, dest, out, ret, [], {}, error=str(e)[:300])
            print(f"[erro] {origin}->{dest} {out} / {ret}: {e}", file=sys.stderr)
            fail += 1
        time.sleep(CFG["sleep_between_calls_s"] if args.provider != "mock" else 0)
    print(f"Rodada concluída: {ok} ok, {fail} erro(s)")


if __name__ == "__main__":
    main()
