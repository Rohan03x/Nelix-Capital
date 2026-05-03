from docx import Document
import re
doc = Document(r'c:/Users/Rohan/Downloads/New folder (4)/Automated Valuation System - Architecture Plan.docx')
full = '\n'.join(p.text for p in doc.paragraphs)
# Find all mentions of ff_data construction / labelling
for m in re.finditer(r'(DCF|EV/EBITDA|comps|52.week|52wk|trading range|precedent|transaction)', full):
    pos = m.start()
    ctx = full[max(0,pos-80):pos+80]
    if any(k in ctx.lower() for k in ['ff_data', 'label', 'football', 'band']):
        print(repr(ctx[:160]))
        print()
