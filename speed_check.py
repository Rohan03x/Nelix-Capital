import sqlite3, json
from pathlib import Path
from datetime import datetime

db = sqlite3.connect('auto_valuation/learning/db/predictions.db')

ms = json.loads(Path('auto_valuation/learning/db/maintenance_state.json').read_text())
rs = json.loads(Path('auto_valuation/learning/db/background_runner_state.json').read_text())

print('bootstrap_last_run_at:', ms.get('bootstrap_last_run_at'))
print('maintenance_last_run_at:', ms.get('maintenance_last_run_at'))
print('runner cycle last_run_at:', rs.get('last_run_at'))
print('bootstrap.ran:', rs.get('bootstrap', {}).get('ran'), '| reason:', rs.get('bootstrap', {}).get('reason'))
print('maintenance.ran:', rs.get('maintenance', {}).get('ran'), '| reason:', rs.get('maintenance', {}).get('reason'))
print()

total_pred = db.execute("SELECT COUNT(*) FROM prediction_records").fetchone()[0]
unique = db.execute("SELECT COUNT(DISTINCT ticker) FROM prediction_records").fetchone()[0]
print(f'Total predictions: {total_pred} | Unique tickers: {unique}')

for window in [10, 30, 60, 120, 240]:
    n = db.execute(f"SELECT COUNT(*) FROM prediction_records WHERE created_at >= datetime('now','-{window} minutes')").fetchone()[0]
    u = db.execute(f"SELECT COUNT(DISTINCT ticker) FROM prediction_records WHERE created_at >= datetime('now','-{window} minutes')").fetchone()[0]
    print(f'  Last {window:3d}min: {n:4d} predictions  {u:3d} unique tickers')

recent = db.execute("SELECT ticker, created_at FROM prediction_records ORDER BY created_at DESC LIMIT 3").fetchall()
print(f'Most recent: {recent}')

rows = db.execute('SELECT created_at FROM prediction_records ORDER BY created_at DESC LIMIT 20').fetchall()
if len(rows) >= 2:
    newest = datetime.fromisoformat(rows[0][0])
    oldest = datetime.fromisoformat(rows[-1][0])
    span = (newest - oldest).total_seconds()
    if span > 0:
        rate_hr = (20 / span) * 3600
        print(f'\nCurrent burst rate (last 20 records): {rate_hr:.0f} predictions/hr')

mrows = db.execute('SELECT started_at, completed_at FROM maintenance_runs ORDER BY started_at DESC LIMIT 6').fetchall()
print('\nLast maintenance run durations:')
for r in mrows:
    try:
        s = datetime.fromisoformat(str(r[0]))
        e = datetime.fromisoformat(str(r[1]))
        print(f'  {str(r[0])[:19]}  {(e-s).total_seconds():.0f}s')
    except Exception:
        pass

runs_today = db.execute("SELECT COUNT(*) FROM maintenance_runs WHERE started_at >= date('now')").fetchone()[0]
print(f'\nMaintenance runs today: {runs_today}')
