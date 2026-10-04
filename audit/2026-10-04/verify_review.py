"""Check report structure, local evidence links and unchanged reviewed inputs."""
import ast
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / 'CLAUDE_CODEX_COLLABORATION.md'
text = DOC.read_text(encoding='utf-8')
errors = []
manifest = json.loads(Path(__file__).with_name('source-manifest.json').read_text(encoding='utf-8'))
for row in manifest['files']:
    path = ROOT / row['path']
    if hashlib.sha256(path.read_bytes()).hexdigest() != row['sha256']:
        errors.append('Reviewed input changed: ' + row['path'])
for target in re.findall(r'\]\(([^)]+)\)', text):
    if target.startswith(('https://', 'http://', '#')):
        continue
    if not (ROOT / target).is_file():
        errors.append('Missing evidence link: ' + target)
ids = re.findall(r'^### (F\d+) ·', text, re.M)
if ids != [f'F{i:02}' for i in range(1, 22)]:
    errors.append('Finding IDs are missing, duplicate or out of order')
if len(re.findall(r'^```', text, re.M)) % 2:
    errors.append('Unbalanced code fences')
snippets = re.findall(r'```python\n(.*?)\n```', text, re.S)
for index, snippet in enumerate(snippets, 1):
    # An elif branch is intentionally shown without its preceding if.
    if snippet.startswith('elif '):
        snippet = 'if False:\n    pass\n' + snippet
    try:
        ast.parse(snippet)
    except SyntaxError as error:
        errors.append(f'Example {index}: {error}')
result = {'reviewed_input_files_unchanged': len(manifest['files']) if not errors else None,
    'finding_count': len(ids), 'python_examples_syntax_checked': len(snippets),
    'report_lines': len(text.splitlines()), 'errors': errors,
    'scope': 'Structure/hash validation only; examples remain proposed implementation sketches.'}
Path(__file__).with_name('review-verification.json').write_text(
    json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(result, ensure_ascii=False, indent=2))
raise SystemExit(bool(errors))
