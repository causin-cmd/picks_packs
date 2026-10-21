#!/usr/bin/env python3
"""
Calcula productividad por operario, bodega y turno.

Definiciones:
  picks     = cantidad de pedidos pickeados (distinct order_id+tote_id)
  picks_uds = unidades pickeadas (sum picked_quantity)
  packs     = cantidad de pedidos empacados (count de filas en packs file)
  packs_uds = unidades empacadas (sum total_items)
  hours     = ventana activa del día (max_ts - min_ts) - 1hr colación,
               mínimo 0.25hr. Solo descuenta colación si la ventana > 5hrs.

Nota: el umbral de colación es 5hrs para alinear con el criterio de Zubale
(verificado 2026-09-01 contra su archivo de productividad: span máximo sin
descuento 4.92hrs, span mínimo con descuento 5.06hrs). Antes era 1.5hrs, lo
que restaba una hora en turnos parciales de 2-5hrs e inflaba el PPH.
"""
import json, os
import pandas as pd
from datetime import datetime, timedelta

HISTORICO_FILE = r"C:\Users\causi\OneDrive\eDS\Claude\productividad_historico.json"

WAREHOUSE_NAMES = {
    104318: "Las Parcelas",
    98824:  "Parque Arauco",
    91609:  "Buenaventura B2C",
    89300:  "Guillermo Prieto B2B",
    85227:  "Guillermo Prieto",
    84853:  "Liquidos B2B",
    84110:  "Liquidos",
    82569:  "Chacabuco",
    73365:  "Vitacura",
}

SHIFTS = [
    "Turno Diurno Semana",
    "Turno Diurno Sábado",
    "Turno Diurno Dom/Feriados",
    "Turno Nocturno",
]

# Feriados Chile 2026 — agregar según calendario oficial
FERIADOS_CL = {
    "2026-01-01", "2026-04-03", "2026-04-04", "2026-05-01",
    "2026-05-21", "2026-06-29", "2026-07-16", "2026-08-15",
    "2026-09-18", "2026-09-19", "2026-10-12", "2026-10-31",
    "2026-11-01", "2026-12-08", "2026-12-25",
}


def _classify_shift(ts):
    """Clasifica un timestamp en uno de los 4 turnos."""
    if pd.isna(ts):
        return "Turno Diurno Semana"
    h = ts.hour
    wd = ts.weekday()  # 0=lun, 5=sab, 6=dom
    date_str = ts.strftime("%Y-%m-%d")
    is_holiday = date_str in FERIADOS_CL

    # Turno nocturno: cualquier día a partir de las 22hrs o antes de las 7hrs
    if h >= 22 or h < 7:
        return "Turno Nocturno"

    # Turnos diurnos (9-19hrs)
    if is_holiday or wd == 6:
        return "Turno Diurno Dom/Feriados"
    if wd == 5:
        return "Turno Diurno Sabado"
    return "Turno Diurno Semana"


_MAX_SHIFT_HRS = 12.0  # tope de seguridad: ventana máxima de un turno (nocturnos extendidos llegan a ~11.5 hrs)
_COLACION_MIN_SPAN = 5.0  # solo se descuenta colación sobre este span (criterio Zubale)


def _day_hours(ts_min, ts_max):
    """
    Horas trabajadas en un turno: ventana activa menos 1hr de colación.
    Solo descuenta colación si la ventana supera 5hrs (_COLACION_MIN_SPAN),
    mismo umbral que usa Zubale.
    Mínimo absoluto: 0.25hrs. Tope: 12hrs (evita ventanas infladas por
    timestamps erróneos en el archivo fuente).
    """
    if pd.isna(ts_min) or pd.isna(ts_max) or ts_min == ts_max:
        return 0.25
    span = min((ts_max - ts_min).total_seconds() / 3600, _MAX_SHIFT_HRS)
    if span > _COLACION_MIN_SPAN:
        span -= 1.0  # descuento colación
    return max(span, 0.25)


_SESSION_GAP_HRS = 3.0  # pausa mínima entre registros de un mismo operario para cortar el turno


def _prep_events(df):
    """Agrega picker_name y parsea created_at."""
    df = df.copy()
    df["picker_name"] = (
        df["user_first_name"].astype(str).str.strip()
        + " "
        + df["user_last_name"].astype(str).str.strip()
    ).str.strip()
    df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce")
    return df


def _assign_sessions(pk, pa):
    """
    Agrupa picks y packs de cada operario (y bodega) en sesiones de trabajo
    continuas: una sesión se corta cuando el mismo operario pasa más de
    _SESSION_GAP_HRS sin registrar actividad.

    Así, un turno nocturno que se extiende después de las 07:00 (p.ej. el
    operario sigue empacando hasta las 10:00) se mantiene como un solo turno,
    siempre que sea el mismo usuario trabajando sin interrupción desde la noche.

    La fecha del turno (shift_date) es la del inicio de la sesión; si la
    sesión empieza antes de las 07:00, pertenece a la noche anterior.
    Escribe las columnas session y shift_date en pk y pa.
    """
    cols = ["picker_name", "warehouse_id", "created_at"]
    ev = pd.concat([
        pk[cols].assign(_src="pk", _idx=pk.index),
        pa[cols].assign(_src="pa", _idx=pa.index),
    ], ignore_index=True)
    ev = ev.sort_values(cols, na_position="last")

    same_user = (ev["picker_name"].eq(ev["picker_name"].shift())
                 & ev["warehouse_id"].eq(ev["warehouse_id"].shift()))
    gap = ev["created_at"].diff() > pd.Timedelta(hours=_SESSION_GAP_HRS)
    ev["session"] = (~same_user | gap | ev["created_at"].isna()).cumsum()

    start = ev.groupby("session")["created_at"].transform("min")
    ev["shift_date"] = start.apply(
        lambda ts: None if pd.isna(ts)
        else (ts.date() - timedelta(days=1) if ts.hour < 7 else ts.date())
    )

    for df, src in ((pk, "pk"), (pa, "pa")):
        sub = ev[ev["_src"] == src].set_index("_idx")
        df["session"] = sub["session"]
        df["shift_date"] = sub["shift_date"]


def build_sessions(picks_df, packs_df):
    """
    Una fila por turno (sesión) de cada operario y bodega, con picks, packs,
    ventana ts_min/ts_max, date (fecha del turno) y shift (tipo de turno).
    La usan calculate() y zubale_diario_update.py.
    """
    empty = pd.DataFrame(columns=["user_first_name", "user_last_name", "warehouse_id", "created_at"])
    pk = _prep_events(picks_df if picks_df is not None and not picks_df.empty else empty)
    pa = _prep_events(packs_df if packs_df is not None and not packs_df.empty else empty)
    _assign_sessions(pk, pa)

    keys = ["picker_name", "warehouse_id", "session", "shift_date"]

    # ── PICKS ──────────────────────────────────────────────────────────────
    if not pk.empty:
        # Clave única de pedido: order_id + tote_id
        pk["order_tote"] = pk["order_id"].astype(str) + "|" + pk["tote_id"].astype(str)

        picks_daily = (
            pk.groupby(keys)
            .agg(
                picks=("order_tote", "nunique"),
                picks_uds=("picked_quantity", "sum"),
                ts_min=("created_at", "min"),
                ts_max=("created_at", "max"),
            )
            .reset_index()
            .rename(columns={"shift_date": "date"})
        )
    else:
        picks_daily = pd.DataFrame(
            columns=["picker_name", "warehouse_id", "session", "date", "picks", "picks_uds", "ts_min", "ts_max"]
        )

    # ── PACKS ──────────────────────────────────────────────────────────────
    if not pa.empty:
        # ShipHero entrega order_tote+picked_quantity; Gmail entrega id+total_items
        _pa_id_col  = "order_tote" if "order_tote" in pa.columns else "id"
        _pa_uds_col = "picked_quantity" if "picked_quantity" in pa.columns and "total_items" not in pa.columns else "total_items"
        packs_daily = (
            pa.groupby(keys)
            .agg(
                packs=(_pa_id_col, "count"),
                packs_uds=(_pa_uds_col, "sum"),
                ts_min=("created_at", "min"),
                ts_max=("created_at", "max"),
            )
            .reset_index()
            .rename(columns={"shift_date": "date"})
        )
    else:
        packs_daily = pd.DataFrame(
            columns=["picker_name", "warehouse_id", "session", "date", "packs", "packs_uds", "ts_min", "ts_max"]
        )

    # ── MERGE por sesión (picks + packs del mismo turno) ───────────────────
    merged = pd.merge(
        picks_daily, packs_daily,
        on=["picker_name", "warehouse_id", "session", "date"],
        how="outer",
        suffixes=("_pk", "_pa"),
    )
    merged["picks"]     = merged["picks"].fillna(0)
    merged["picks_uds"] = merged["picks_uds"].fillna(0)
    merged["packs"]     = merged["packs"].fillna(0)
    merged["packs_uds"] = merged["packs_uds"].fillna(0)

    # Ventana temporal combinada (picks + packs de la misma sesión)
    merged["ts_min"] = merged[["ts_min_pk", "ts_min_pa"]].min(axis=1)
    merged["ts_max"] = merged[["ts_max_pk", "ts_max_pa"]].max(axis=1)

    # Clasificar turno usando ts_min Y ts_max:
    # - Si el turno cruza medianoche (ts_max es del día siguiente) → Nocturno
    # - Si ts_min está en madrugada (< 07:00) → Nocturno
    # - Si no, clasificar por hora de ts_min y día de semana
    def _classify_row(row):
        ts_min, ts_max = row["ts_min"], row["ts_max"]
        if pd.notna(ts_min) and pd.notna(ts_max):
            try:
                if ts_max.date() > ts_min.date() and ts_min.hour >= 20:
                    return "Turno Nocturno"
            except Exception:
                pass
        return _classify_shift(ts_min)

    merged["shift"] = merged.apply(_classify_row, axis=1)
    return merged


def calculate(picks_df, packs_df):
    """
    Devuelve lista de dicts agrupados por (picker_name, warehouse_id, shift).
    """
    merged = build_sessions(picks_df, packs_df)

    merged["hours"] = merged.apply(
        lambda r: _day_hours(r["ts_min"], r["ts_max"]), axis=1
    )

    # ── AGREGAR por operario + bodega + día + turno (granularidad diaria) ─
    summary = (
        merged.groupby(["picker_name", "warehouse_id", "date", "shift"])
        .agg(
            picks=("picks", "sum"),
            picks_uds=("picks_uds", "sum"),
            packs=("packs", "sum"),
            packs_uds=("packs_uds", "sum"),
            hours=("hours", "sum"),
        )
        .reset_index()
    )

    summary["picks_uph"] = (summary["picks_uds"] / summary["hours"].replace(0, float("nan"))).round(1)
    summary["packs_uph"] = (summary["packs_uds"] / summary["hours"].replace(0, float("nan"))).round(1)
    summary["warehouse_name"] = summary["warehouse_id"].map(
        lambda x: WAREHOUSE_NAMES.get(int(x), str(int(x)))
    )

    # Ordenar: fecha asc, bodega asc, turno, picks_uph desc
    summary = summary.sort_values(
        ["date", "warehouse_id", "shift", "picks_uph"], ascending=[True, True, True, False]
    )

    results = []
    for _, row in summary.iterrows():
        results.append({
            "date":           str(row["date"]),
            "warehouse_id":   int(row["warehouse_id"]),
            "warehouse_name": row["warehouse_name"],
            "picker_name":    row["picker_name"],
            "shift":          row["shift"],
            "picks":          int(row["picks"]),
            "picks_uds":      int(row["picks_uds"]),
            "packs":          int(row["packs"]),
            "packs_uds":      int(row["packs_uds"]),
            "hours":          round(float(row["hours"]), 2),
            "picks_uph":      None if pd.isna(row["picks_uph"]) else round(float(row["picks_uph"]), 1),
            "packs_uph":      None if pd.isna(row["packs_uph"]) else round(float(row["packs_uph"]), 1),
        })

    # ── AGRE DIARIO por fecha + bodega + turno (para gráfico de tendencia) ──
    daily_agg = (
        merged.groupby(["date", "warehouse_id", "shift"])
        .agg(
            picks=("picks", "sum"),
            picks_uds=("picks_uds", "sum"),
            packs=("packs", "sum"),
            packs_uds=("packs_uds", "sum"),
            hours=("hours", "sum"),
        )
        .reset_index()
    )
    daily_agg["picks_uph"] = (daily_agg["picks_uds"] / daily_agg["hours"].replace(0, float("nan"))).round(1)
    daily_agg["packs_uph"] = (daily_agg["packs_uds"] / daily_agg["hours"].replace(0, float("nan"))).round(1)
    daily_agg["picks_phr"] = (daily_agg["picks"] / daily_agg["hours"].replace(0, float("nan"))).round(1)
    daily_agg["packs_phr"] = (daily_agg["packs"] / daily_agg["hours"].replace(0, float("nan"))).round(1)
    daily_agg["warehouse_name"] = daily_agg["warehouse_id"].map(
        lambda x: WAREHOUSE_NAMES.get(int(x), str(int(x)))
    )
    daily_agg = daily_agg.sort_values(["date", "warehouse_id", "shift"])

    daily_records = []
    for _, row in daily_agg.iterrows():
        daily_records.append({
            "date":           str(row["date"]),
            "warehouse_id":   int(row["warehouse_id"]),
            "warehouse_name": row["warehouse_name"],
            "shift":          row["shift"],
            "picks":          int(row["picks"]),
            "picks_uds":      int(row["picks_uds"]),
            "packs":          int(row["packs"]),
            "packs_uds":      int(row["packs_uds"]),
            "hours":          round(float(row["hours"]), 2),
            "picks_uph":      None if pd.isna(row["picks_uph"]) else round(float(row["picks_uph"]), 1),
            "packs_uph":      None if pd.isna(row["packs_uph"]) else round(float(row["packs_uph"]), 1),
            "picks_phr":      None if pd.isna(row["picks_phr"]) else round(float(row["picks_phr"]), 1),
            "packs_phr":      None if pd.isna(row["packs_phr"]) else round(float(row["packs_phr"]), 1),
        })

    return {"data": results, "daily": daily_records}


def update_historico(month_str, result):
    historico = {}
    if os.path.exists(HISTORICO_FILE):
        with open(HISTORICO_FILE, encoding="utf-8") as f:
            historico = json.load(f)
    if isinstance(result, dict):
        # Los archivos mensuales se traslapan en el cambio de mes (el de septiembre
        # trae la madrugada del 1/10 y el de octubre la noche del 30/09 a medias).
        # Cada mes guarda solo los turnos cuya fecha es de ese mes.
        in_month = lambda recs: [r for r in recs if r["date"].startswith(month_str)]
        historico[month_str] = {
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "data":  in_month(result.get("data", [])),
            "daily": in_month(result.get("daily", [])),
        }
    else:
        # Compatibilidad con formato antiguo (lista plana)
        historico[month_str] = {
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "data": result,
        }
    with open(HISTORICO_FILE, "w", encoding="utf-8") as f:
        json.dump(historico, f, ensure_ascii=False, indent=2)
    return historico


def load_historico():
    if not os.path.exists(HISTORICO_FILE):
        return {}
    with open(HISTORICO_FILE, encoding="utf-8") as f:
        return json.load(f)


CPO_DATA_FILE = r"C:\Users\causi\OneDrive\eDS\Claude\cpo_data.json"


def sync_historico_to_cpo_data(historico):
    """
    Copia el histórico de productividad al campo 'productividad' de cpo_data.json,
    sin necesitar el Excel fuente. Si cpo_data.json no existe, no hace nada.
    """
    if not os.path.exists(CPO_DATA_FILE):
        return False
    try:
        with open(CPO_DATA_FILE, encoding="utf-8") as f:
            cpo_data = json.load(f)
        cpo_data["productividad"] = historico
        with open(CPO_DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(cpo_data, f, ensure_ascii=False, default=str)
        return True
    except Exception as e:
        print(f"AVISO: no se pudo sincronizar cpo_data.json: {e}")
        return False
