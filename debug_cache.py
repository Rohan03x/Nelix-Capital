"""Quick cache diagnostic."""
import json, pathlib

cache_dir = pathlib.Path("webapp/data/cache")
for f in sorted(cache_dir.glob("eodhd_fund_*.json")):
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        ts = d.get("_ts", "?")
        data = d.get("data", {})
        yearly = data.get("Financials", {}).get("Income_Statement", {}).get("yearly", {})
        years = sorted(yearly.keys())
        n = len(years)
        latest = years[-1][:7] if years else "?"
        first = years[0][:7] if years else "?"
        # Get revenue for each year
        revs = []
        for yr in years:
            r = float(yearly[yr].get("totalRevenue") or 0)
            revs.append((yr[:4], round(r / 1e9, 1)))
        print(f"{f.name}")
        print(f"  cached={ts[:10]}, n={n}, {first}..{latest}")
        if len(revs) >= 2:
            first_rev = revs[0][1]
            last_rev = revs[-1][1]
            n_periods = len(revs) - 1
            if first_rev > 0 and n_periods > 0:
                cagr = (last_rev / first_rev) ** (1 / n_periods) - 1
                print(f"  Full CAGR: {cagr*100:.1f}% (from {revs[0][0]}:{first_rev}B to {revs[-1][0]}:{last_rev}B)")
            recent = revs[-6:] if len(revs) > 6 else revs
            if len(recent) >= 2 and recent[0][1] > 0:
                ry = len(recent) - 1
                rc = (recent[-1][1] / recent[0][1]) ** (1 / ry) - 1
                print(f"  Recent CAGR ({recent[0][0]}-{recent[-1][0]}): {rc*100:.1f}%")
            last_g = (revs[-1][1] / revs[-2][1] - 1) * 100 if len(revs) >= 2 and revs[-2][1] > 0 else 0
            print(f"  Last YoY: {last_g:.1f}%")
    except Exception as e:
        print(f"{f.name}: ERROR {e}")
