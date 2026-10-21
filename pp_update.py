#!/usr/bin/env python3
"""
Actualiza data/data.json del dashboard Picks & Packs (bodegas eDarkstore) desde la API
de ShipHero. Lo corre GitHub Actions cada hora 9:30-18:30 (hora Chile); no depende de
Claude ni de un PC.

Reglas: productividad_calc.py es una COPIA EXACTA del módulo del dashboard CPO D2C, así que
picks, packs, sesiones, horas (colación solo sobre 5h, piso 0,25h, tope 12h), turnos y
fecha de turno nocturno son los mismos. Si cambia allá, copiarlo de nuevo acá.

Formato de la API (igual que shiphero_productividad.py del CPO):
  - created_at viene en UTC; se pasa a la hora local de cada bodega
    (Chile, salvo Guillermo Prieto y su B2B en America/Mexico_City).
  - date_from/date_to se interpretan en hora de Chile aunque lleven "Z".
  - IDs vienen en base64 ("Order:123").

Cada corrida baja los últimos días (hora Chile) y reemplaza en el histórico los turnos con
fecha desde ayer (o desde la última corrida, si quedó atrás) hasta hoy.

Credenciales: variables de entorno SHIPHERO_USERNAME y SHIPHERO_PASSWORD (secrets del repo).
En el PC, sin esas variables, usa shiphero_picks_packs.py de la carpeta del CPO (keyring).

Uso:
  python pp_update.py            # respeta la ventana horaria (para el cron)
  python pp_update.py --force    # corre a cualquier hora
"""
import base64, json, os, sys, time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from productividad_calc import calculate

BASE = Path(__file__).resolve().parent
DATA_FILE = BASE / "data" / "data.json"
CL = ZoneInfo("America/Santiago")
WAREHOUSE_TZ = {85227: "America/Mexico_City", 89300: "America/Mexico_City"}  # resto: Chile
EXCLUIDAS = {84853, 73365}  # Liquidos B2B, Vitacura: igual que Productividad Producción del CPO
MESES_GUARDADOS = 3          # mes actual + 2 anteriores para la vista Semana/Mes
MAX_DIAS_RECUPERAR = 31

# El cron de GitHub corre a los :30 de 12 a 22 UTC (cubre horario de verano e invierno);
# esta ventana descarta las corridas que caen fuera de 9:30-18:30 Chile. Holgada porque
# GitHub a veces atrasa el cron varios minutos.
VENTANA = ((9, 0), (19, 25))

AUTH_URL = "https://public-api.shiphero.com/auth/token"
GQL_URL = "https://public-api.shiphero.com/graphql"

_PICKS = """
query($df:ISODateTime,$dt:ISODateTime,$after:String){
  picks_per_day(date_from:$df, date_to:$dt){
    data(first:1000, after:$after){
      pageInfo{hasNextPage endCursor}
      edges{node{user_first_name user_last_name warehouse_id tote_id order_id
                 picked_quantity created_at}}
    }
  }
}"""

_PACKS = """
query($df:ISODateTime,$dt:ISODateTime,$after:String){
  packs_per_day(date_from:$df, date_to:$dt){
    data(first:1000, after:$after){
      pageInfo{hasNextPage endCursor}
      edges{node{id user_first_name user_last_name warehouse_id total_items created_at}}
    }
  }
}"""


def log(msg):
    print(f"[{datetime.now(CL):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# ShipHero
# ---------------------------------------------------------------------------

def _token():
    user, pwd = os.environ.get("SHIPHERO_USERNAME"), os.environ.get("SHIPHERO_PASSWORD")
    if user and pwd:
        r = requests.post(AUTH_URL, json={"username": user, "password": pwd}, timeout=30)
        if not r.ok:
            raise RuntimeError(f"Auth ShipHero falló {r.status_code}: {r.text[:300]}")
        return r.json()["access_token"]
    # En el PC: mismo cliente y credenciales (keyring) que el dashboard CPO
    sys.path.insert(0, str(BASE.parent))
    from shiphero_picks_packs import _get_token
    return _get_token()


def _gql(query, variables, token, tries=5):
    for i in range(tries):
        try:
            r = requests.post(GQL_URL, json={"query": query, "variables": variables},
                              headers={"Authorization": f"Bearer {token}"}, timeout=90)
            body = r.json() if r.ok else None
            if body is not None and "errors" not in body:
                return body["data"]
            msg = str(body["errors"]) if body else f"HTTP {r.status_code}: {r.text[:300]}"
            retry = any(s in msg.lower() for s in ("credit", "throttl", "429", "timeout", "502", "503", "504"))
        except requests.RequestException as e:
            msg, retry = f"red: {e}", True
        if not retry or i == tries - 1:
            raise RuntimeError(f"ShipHero: {msg}")
        log(f"  reintento en {30 * (i + 1)}s ({msg[:120]})")
        time.sleep(30 * (i + 1))


def _decode_id(encoded):
    if not encoded:
        return encoded
    try:
        part = base64.b64decode(encoded + "==").decode().split(":")[-1]
        return int(part) if part.isdigit() else part
    except Exception:
        return encoded


def _pull(token, query, name, d):
    # La API interpreta estas fechas en hora de Chile: se pasan como "hora local".
    variables = {"df": f"{d.isoformat()}T00:00:00Z", "dt": f"{(d + timedelta(days=1)).isoformat()}T00:00:00Z"}
    rows, after = [], None
    while True:
        data = _gql(query, {**variables, "after": after}, token)[name]["data"]
        rows += [e["node"] for e in data["edges"]]
        if not data["pageInfo"]["hasNextPage"]:
            return rows
        after = data["pageInfo"]["endCursor"]


def _to_local(created_utc, wh_ids):
    utc = pd.to_datetime(created_utc, utc=True)
    out = pd.Series(pd.NaT, index=utc.index, dtype="datetime64[ns]")
    tz_per_row = wh_ids.map(lambda w: WAREHOUSE_TZ.get(int(w), "America/Santiago"))
    for tz, idx in tz_per_row.groupby(tz_per_row).groups.items():
        out.loc[idx] = utc.loc[idx].dt.tz_convert(tz).dt.tz_localize(None).values
    return out


def fetch(d0, d1):
    """(picks, packs) de los días Chile d0..d1, en el formato que usa productividad_calc."""
    token = _token()
    pks, pas = [], []
    d = d0
    while d <= d1:
        pk = _pull(token, _PICKS, "picks_per_day", d)
        pa = _pull(token, _PACKS, "packs_per_day", d)
        log(f"  {d}: {len(pk)} picks, {len(pa)} packs")
        pks += pk
        pas += pa
        d += timedelta(days=1)

    pk = pd.DataFrame(pks)
    if not pk.empty:
        pk = pd.DataFrame({
            "user_first_name": pk["user_first_name"].fillna(""),
            "user_last_name":  pk["user_last_name"].fillna(""),
            "warehouse_id":    pk["warehouse_id"].map(_decode_id).astype("int64"),
            "order_id":        pk["order_id"].map(_decode_id),
            "tote_id":         pk["tote_id"].map(_decode_id),
            "picked_quantity": pk["picked_quantity"].fillna(0).astype("int64"),
            "created_utc":     pk["created_at"],
        })
    pa = pd.DataFrame(pas)
    if not pa.empty:
        pa = pd.DataFrame({
            "id":              pa["id"].map(_decode_id),
            "user_first_name": pa["user_first_name"].fillna(""),
            "user_last_name":  pa["user_last_name"].fillna(""),
            "warehouse_id":    pa["warehouse_id"].map(_decode_id).astype("int64"),
            "total_items":     pa["total_items"].fillna(0).astype("int64"),
            "created_utc":     pa["created_at"],
        })
    out = []
    for df in (pk, pa):
        if df.empty:
            out.append(None)
            continue
        df = df[~df["warehouse_id"].isin(EXCLUIDAS)].copy()
        df["created_at"] = _to_local(df.pop("created_utc"), df["warehouse_id"])
        out.append(df.reset_index(drop=True))
    return out[0], out[1]


# ---------------------------------------------------------------------------
# Avance por hora del día
# ---------------------------------------------------------------------------

def hourly(pk, pa, day):
    """
    Picks/packs por bodega y hora local de los turnos con fecha `day` (gráfico de avance de
    Hoy). Usa la misma asignación de sesiones que productividad_calc, así las marcas de la
    madrugada quedan en el turno nocturno del día anterior, igual que en la tabla.
    """
    from productividad_calc import WAREHOUSE_NAMES, _prep_events, _assign_sessions
    empty = pd.DataFrame(columns=["user_first_name", "user_last_name", "warehouse_id", "created_at"])
    pk = _prep_events(pk if pk is not None else empty)
    pa = _prep_events(pa if pa is not None else empty)
    _assign_sessions(pk, pa)
    pk = pk[pk["shift_date"] == day] if not pk.empty else None
    pa = pa[pa["shift_date"] == day] if not pa.empty else None
    acc = {}

    def bucket(wh, h):
        k = (int(wh), int(h))
        if k not in acc:
            acc[k] = {"warehouse_id": k[0], "warehouse_name": WAREHOUSE_NAMES.get(k[0], str(k[0])),
                      "hour": k[1], "picks": 0, "picks_uds": 0, "packs": 0, "packs_uds": 0}
        return acc[k]

    if pk is not None:
        p = pk.copy()
        p["order_tote"] = p["order_id"].astype(str) + "|" + p["tote_id"].astype(str)
        p["hour"] = p["created_at"].dt.hour
        # Un pedido cuenta en la hora de su primera marca de pick
        first = p.sort_values("created_at").drop_duplicates(["warehouse_id", "order_tote"])
        for (wh, h), n in first.groupby(["warehouse_id", "hour"]).size().items():
            bucket(wh, h)["picks"] += int(n)
        for (wh, h), n in p.groupby(["warehouse_id", "hour"])["picked_quantity"].sum().items():
            bucket(wh, h)["picks_uds"] += int(n)
    if pa is not None:
        p = pa.copy()
        p["hour"] = p["created_at"].dt.hour
        for (wh, h), g in p.groupby(["warehouse_id", "hour"]):
            b = bucket(wh, h)
            b["packs"] += len(g)
            b["packs_uds"] += int(g["total_items"].sum())
    return sorted(acc.values(), key=lambda r: (r["warehouse_id"], r["hour"]))


def last_events(pk, pa):
    """Hora de la última marca (pick o pack) por operario-bodega, para el indicador 'activo'."""
    frames = []
    for df in (pk, pa):
        if df is not None and not df.empty:
            frames.append(df[["user_first_name", "user_last_name", "warehouse_id", "created_at"]])
    if not frames:
        return {}
    ev = pd.concat(frames, ignore_index=True)
    ev["picker_name"] = (ev["user_first_name"].astype(str).str.strip() + " "
                         + ev["user_last_name"].astype(str).str.strip()).str.strip()
    last = ev.groupby(["picker_name", "warehouse_id"])["created_at"].max()
    return {f"{n}||{int(w)}": ts.strftime("%Y-%m-%d %H:%M") for (n, w), ts in last.items()}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_data():
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return {"months": {}}


def main():
    now = datetime.now(CL)
    if "--force" not in sys.argv:
        hm = (now.hour, now.minute)
        if not (VENTANA[0] <= hm < VENTANA[1]):
            log(f"Fuera de la ventana 9:30-18:30 Chile ({now:%H:%M}); no se actualiza.")
            return

    data = load_data()
    today = now.date()
    desde = today - timedelta(days=1)
    last_run = (data.get("generated") or "")[:10]
    if last_run:
        prev = date.fromisoformat(last_run) - timedelta(days=1)
        desde = max(min(desde, prev), today - timedelta(days=MAX_DIAS_RECUPERAR))
    else:
        desde = today.replace(day=1)
    # Un día extra antes: los turnos que empiezan la noche anterior y la hora de México
    fetch_from = desde - timedelta(days=1)

    log(f"=== Picks & Packs: turnos {desde} a {today} (baja desde {fetch_from}) ===")
    pk, pa = fetch(fetch_from, today)
    result = calculate(pk, pa)

    keep = lambda recs: [r for r in recs if desde.isoformat() <= r["date"] <= today.isoformat()]
    new_data, new_daily = keep(result["data"]), keep(result["daily"])

    months = data.get("months", {})
    for m in sorted({r["date"][:7] for r in new_data + new_daily} | {today.strftime("%Y-%m")}):
        cur = months.setdefault(m, {"data": [], "daily": []})
        for k, recs in (("data", new_data), ("daily", new_daily)):
            old = [r for r in cur.get(k, []) if not (desde.isoformat() <= r["date"] <= today.isoformat())]
            cur[k] = sorted(old + [r for r in recs if r["date"].startswith(m)],
                            key=lambda r: (r["date"], r["warehouse_id"], r["shift"]))
    for m in sorted(months)[:-MESES_GUARDADOS]:
        del months[m]

    data = {
        "generated": now.strftime("%Y-%m-%d %H:%M"),
        "today": today.isoformat(),
        "months": months,
        "hourly": hourly(pk, pa, today),
        "last_event": last_events(pk, pa),
    }
    DATA_FILE.parent.mkdir(exist_ok=True)
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    n_today = sum(1 for r in new_data if r["date"] == today.isoformat())
    log(f"OK: {len(new_data)} registros operario-turno ({n_today} de hoy), meses {sorted(months)}")


if __name__ == "__main__":
    main()
