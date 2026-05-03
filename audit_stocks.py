"""Quick audit of multiple stocks."""
from webapp.data.samples import get_dashboard_data

tickers = ['NVDA', 'GOOGL', 'META', 'AMZN', 'BRK-B', 'V', 'JPM', 'NEE', 'NKE', 'SHEL.L']
for ticker in tickers:
    d = get_dashboard_data(ticker)
    if d:
        price = d.get('price', 0)
        iv = d.get('intrinsic_value', 0)
        upside = d.get('upside_pct', 0)
        wacc = d.get('wacc', 0)
        rg = d.get('revenue_growth_near', 0)
        mb = d.get('ebit_margin_base', 0)
        mt = d.get('ebit_margin_target', 0)
        conf = d.get('confidence_score', 0)
        beta = d.get('beta', 0)
        # Check consensus delta
        ac = d.get('analyst_consensus', {})
        cons_rev = ac.get('revenue_y1_consensus', 0)
        model_rev = ac.get('revenue_y1_model', 0)
        delta = (model_rev - cons_rev) / cons_rev * 100 if cons_rev else 0
        
        print(f'{ticker}:')
        print(f'  price=${price}, iv=${iv}, upside={upside}%')
        print(f'  wacc={wacc}%, beta={beta}, rev_growth={rg}%, margin {mb}%->{mt}%')
        print(f'  confidence={conf}, consensus_delta={delta:.1f}%')
        print()
    else:
        print(f'{ticker}: FAILED\n')
