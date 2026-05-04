import sqlite3, statistics

# Find active DB and table
db_path = None
conn = None
pred_table = None
for candidate in ['auto_valuation/learning/db/predictions.db', 'learning/db/predictions.db']:
    c = sqlite3.connect(candidate)
    c.row_factory = sqlite3.Row
    tables = [r[0] for r in c.execute('SELECT name FROM sqlite_master WHERE type=?', ('table',)).fetchall()]
    print(f'DB: {candidate} => tables: {tables}')
    if tables:
        conn = c
        db_path = candidate
        pred_table = next((t for t in tables if 'pred' in t.lower()), tables[0])
        break
    c.close()

if conn is None:
    raise SystemExit('No DB found with tables')

print(f'Using: {db_path} / {pred_table}\n')

# ── 1. High-level counts
row = conn.execute(f'''
    SELECT
        COUNT(*) total,
        SUM(CASE WHEN actual_price_at_horizon IS NOT NULL THEN 1 ELSE 0 END) labeled,
        SUM(CASE WHEN actual_revenue_mm IS NOT NULL AND actual_price_at_horizon IS NULL THEN 1 ELSE 0 END) partial,
        SUM(CASE WHEN actual_revenue_mm IS NULL AND actual_price_at_horizon IS NULL THEN 1 ELSE 0 END) blank
    FROM {pred_table}
''').fetchone()
print("=== LEARNING DB (May 4 2026) ===")
print("Total records :", row['total'])
print("Fully labeled :", row['labeled'])
print("Partial       :", row['partial'])
print("Unlabeled     :", row['blank'])

# ── 2. Sector breakdown
print("\n=== BY SECTOR ===")
q = f'SELECT sector, COUNT(*) cnt, SUM(CASE WHEN actual_price_at_horizon IS NOT NULL THEN 1 ELSE 0 END) lbl FROM {pred_table} GROUP BY sector ORDER BY cnt DESC'
for r in conn.execute(q).fetchall():
    sector = r['sector'] or 'Unknown'
    print(f'  {sector:<30} total={r["cnt"]:>4}  labeled={r["lbl"]:>4}')

# ── 3. Ticker count
tickers = conn.execute(f'SELECT COUNT(DISTINCT ticker) FROM {pred_table}').fetchone()[0]
print(f'\nUnique tickers: {tickers}')

# ── 4. IV Accuracy
rows = conn.execute(f'SELECT predicted_price_per_share, actual_price_at_horizon FROM {pred_table} WHERE actual_price_at_horizon IS NOT NULL AND predicted_price_per_share > 0 AND actual_price_at_horizon > 0').fetchall()
errors = [abs(r[0] - r[1]) / r[1] for r in rows]
if errors:
    w10 = sum(1 for e in errors if e <= 0.10)
    w20 = sum(1 for e in errors if e <= 0.20)
    w30 = sum(1 for e in errors if e <= 0.30)
    print(f'\n=== IV ACCURACY vs ACTUAL PRICE (n={len(errors)}) ===')
    print(f'  MAE:           {statistics.mean(errors)*100:.1f}%')
    print(f'  Median AE:     {statistics.median(errors)*100:.1f}%')
    print(f'  Within +-10%:  {w10}/{len(errors)} ({w10/len(errors)*100:.0f}%)')
    print(f'  Within +-20%:  {w20}/{len(errors)} ({w20/len(errors)*100:.0f}%)')
    print(f'  Within +-30%:  {w30}/{len(errors)} ({w30/len(errors)*100:.0f}%)')

# ── 5. Top tickers
print('\n=== TOP TICKERS BY RECORDS ===')
q2 = f'SELECT ticker, COUNT(*) cnt FROM {pred_table} GROUP BY ticker ORDER BY cnt DESC LIMIT 20'
for r in conn.execute(q2).fetchall():
    print(f'  {r["ticker"]:<15} {r["cnt"]} records')

# ── 6. Most recent
print('\n=== MOST RECENT PREDICTIONS (last 10) ===')
q3 = f'SELECT ticker, run_date, predicted_price_per_share, actual_price_at_horizon FROM {pred_table} ORDER BY created_at DESC LIMIT 10'
for r in conn.execute(q3).fetchall():
    actual = r['actual_price_at_horizon']
    act_str = f'{actual:.2f}' if actual else 'pending'
    ts = str(r['run_date'])[:16]
    print(f'  {r["ticker"]:<12} {ts}  iv={r["predicted_price_per_share"]:.2f}  actual={act_str}')

conn.close()

# ── 7. Global overlay
print('\n=== GLOBAL OVERLAY (what the model has learned) ===')
try:
    from auto_valuation.learning.knowledge_model import _global_cross_symbol_overlay
    overlay = _global_cross_symbol_overlay()
    for k, v in overlay.items():
        print(f'  {k}: {v}')
except Exception as e:
    print(f'  (error: {e})')

# ── 8. Layer weights for AAPL
print('\n=== LAYER WEIGHTS (AAPL) ===')
try:
    from auto_valuation.learning.knowledge_model import refine_live_assumptions
    from auto_valuation.assumptions.defaults import build_default_assumptions
    base = build_default_assumptions('AAPL', 280.14, 'Technology', 'US')
    result = refine_live_assumptions('AAPL', base, 280.14, 'Technology', 'US')
    weights = result.get('_layer_weights') or result.get('layer_weights') or {}
    for k, v in weights.items():
        print(f'  {k}: {v}')
    if not weights:
        print('  (no layer_weights key in result, keys:', list(result.keys())[:10], ')')
except Exception as e:
    print(f'  (error: {e})')

# ── 9. Supabase sync status
print('\n=== SUPABASE NAMESPACES (last sync) ===')
try:
    from auto_valuation.learning.supabase_sync import list_namespaces_status
    for ns in list_namespaces_status():
        print(f'  {ns}')
except Exception as e:
    print(f'  (error: {e})')
