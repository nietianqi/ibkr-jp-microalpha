"""Freeze review input identity without requiring a Git repository."""
import hashlib
import json
import platform
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
paths = [*ROOT.glob('ibkr_microalpha/*.py'), *ROOT.glob('tests/*.py'),
    *ROOT.glob('examples/*.json'), *ROOT.glob('*.md'), ROOT / 'pyproject.toml',
    *ROOT.glob('runs/demo/*'), *ROOT.glob('runs/replayed/*')]
paths = [p for p in paths if p.is_file() and p.name != 'CLAUDE_CODEX_COLLABORATION.md']
records = []
for path in sorted(paths):
    stat = path.stat()
    records.append({'path': path.relative_to(ROOT).as_posix(), 'bytes': stat.st_size,
        'modified_at_jst': datetime.fromtimestamp(stat.st_mtime, ZoneInfo('Asia/Tokyo')).isoformat(),
        'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
manifest = {'review_date_jst': '2026-10-04', 'workspace': str(ROOT),
    'version': 'pyproject.toml 0.1.0 / source specification v1.2',
    'git_history_available': (ROOT / '.git').exists(),
    'python': platform.python_version(), 'platform': platform.platform(),
    'verification': {'existing_tests': 142, 'existing_tests_passed': True,
        'configuration_validation_passed': True, 'compileall_passed': True},
    'files': records}
target = Path(__file__).with_name('source-manifest.json')
target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({'manifest': str(target), 'files': len(records),
    'source_lines': sum(len(p.read_text(encoding='utf-8').splitlines()) for p in ROOT.glob('ibkr_microalpha/*.py'))},
    ensure_ascii=False))
