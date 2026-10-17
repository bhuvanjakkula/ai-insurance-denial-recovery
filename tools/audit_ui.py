"""Static audit of the frontend: every inline handler must exist in app.js and every
getElementById/$() target must exist in index.html. Run: python tools/audit_ui.py"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
html = (ROOT / 'frontend' / 'index.html').read_text(encoding='utf-8')
js = (ROOT / 'frontend' / 'app.js').read_text(encoding='utf-8')

defined = set(re.findall(r'(?:async\s+)?function\s+(\w+)\s*\(', js))
handlers = re.findall(r'\bon(?:click|change|input|keyup|submit)="([^"]+)"', html)
# handlers injected from JS template strings too
handlers += re.findall(r'\bon(?:click|change|input)="([^"]+)"', js)

problems = []
builtins = {'this', 'event', 'return', 'false', 'true', 'document', 'window', 'if', 'click', 'getElementById'}
for h in handlers:
    for name in re.findall(r'\b([A-Za-z_]\w*)\s*\(', h):
        if name not in defined and name not in builtins and not name[0].isupper():
            problems.append(f'handler calls undefined function: {name}()  in  {h[:60]}')

html_ids = re.findall(r'\bid="([^"]+)"', html)
dups = {i for i in html_ids if html_ids.count(i) > 1}
for d in sorted(dups):
    problems.append(f'duplicate id in HTML: {d}')

ids = set(html_ids)
for m in re.findall(r"\$\('([\w\-]+)'\)", js) + re.findall(r"getElementById\('([\w\-]+)'\)", js):
    if m not in ids:
        problems.append(f"JS references missing element id: {m}")

print(f'{len(handlers)} handlers, {len(defined)} functions, {len(ids)} ids checked')
for p in sorted(set(problems)):
    print(' -', p)
print('OK' if not problems else f'{len(set(problems))} problem(s)')
sys.exit(1 if problems else 0)
