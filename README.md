# Picks & Packs · Bodegas eDarkstore

Dashboard de productividad de picking y packing para los equipos de bodega, con datos de la
API de ShipHero. Se actualiza solo cada hora entre 9:30 y 18:30 (hora Chile) con GitHub
Actions: no depende de Claude ni de un PC prendido.

## Links

- Dashboard: https://causin-cmd.github.io/picks_packs/
- Pantalla de una bodega (sin filtros, letra grande):
  `https://causin-cmd.github.io/picks_packs/?modo=hoy&bodega=Las%20Parcelas&tv=1`
- Parámetros: `modo` = hoy | ayer | semana | mes · `bodega` = nombre(s) separados por coma ·
  `empresa` = propios | zubale · `tv=1`. La página recarga los datos cada 5 minutos.

## Reglas

`productividad_calc.py` es una copia exacta del módulo del dashboard CPO D2C
(Productividad Producción). Si allá cambia una regla, copiar el archivo de nuevo acá.
Validado el 21-oct-2026: turnos 18–20 oct idénticos al CPO (55 registros operario-turno,
0 diferencias en picks, packs y horas).

## Piezas

- `pp_update.py` — baja picks/packs de ShipHero, calcula y reemplaza en `data/data.json`
  los turnos desde ayer (o desde la última corrida, si quedó atrás) hasta hoy. Guarda el mes
  actual y los dos anteriores.
- `index.html` — la página (estática, lee `data/data.json`).
- `.github/workflows/update.yml` — cron horario (copia de referencia: `workflow_update.yml`). Ejecutar a mano: pestaña Actions →
  "Actualizar picks y packs" → Run workflow.
- Secrets del repo: `SHIPHERO_USERNAME`, `SHIPHERO_PASSWORD`. Si se cambia la clave de
  ShipHero, actualizarla en Settings → Secrets and variables → Actions.

En el PC: `python pp_update.py --force` (usa las credenciales del keyring del CPO).
