import sqlite3
from pathlib import Path

pred_db = sqlite3.connect('auto_valuation/learning/db/predictions.db')
univ_db = sqlite3.connect('auto_valuation/learning/db/symbol_universe.db')

tables = [r[0] for r in univ_db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print('Universe DB tables:', tables)
if tables:
    t = tables[0]
    n = univ_db.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
    cols = [r[1] for r in univ_db.execute(f'PRAGMA table_info({t})').fetchall()]
    print(f'{t}: {n} rows, cols: {cols}')

# Tickers with predictions
with_preds = pred_db.execute('SELECT COUNT(DISTINCT ticker) FROM prediction_records').fetchone()[0]
total_preds = pred_db.execute('SELECT COUNT(*) FROM prediction_records').fetchone()[0]
print(f'\nTickers with predictions: {with_preds}')
print(f'Total predictions: {total_preds}')
print(f'Avg per ticker: {total_preds/max(with_preds,1):.1f}')

dist = pred_db.execute(
    'SELECT predictions_per_ticker, COUNT(*) as tickers FROM '
    '(SELECT ticker, COUNT(*) as predictions_per_ticker FROM prediction_records GROUP BY ticker) '
    'GROUP BY predictions_per_ticker ORDER BY predictions_per_ticker'
).fetchall()
print('Prediction count distribution:')
for row in dist:
    print(f'  {row[0]} preds: {row[1]} tickers')

at_cap = pred_db.execute(
    'SELECT COUNT(*) FROM (SELECT ticker FROM prediction_records GROUP BY ticker HAVING COUNT(*) >= 5)'
).fetchone()[0]
print(f'Tickers at >=5 cap: {at_cap}')
