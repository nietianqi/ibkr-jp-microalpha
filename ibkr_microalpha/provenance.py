"""Availability and content binding for frozen research parameters.

An artifact citation is not proof of profitability. These checks only prevent
using parameters before their recorded availability or relabelling stale
citations after changing the executable configuration.
"""
from copy import deepcopy
from datetime import datetime
import hashlib
import json

from .domain import aware

PARAMETER_GROUPS = ('scalers', 'thresholds', 'economics')


def parameter_content_hash(document, group):
    if group not in PARAMETER_GROUPS:
        raise ValueError('unknown frozen parameter group')
    if group == 'scalers':
        payload = document['scalers']
    elif group == 'economics':
        payload = {'commissions': document['commissions'], 'engine': document['engine']}
    else:
        # Bind every non-economic executable setting, including the universe,
        # feature definitions, optional market controls and score weights.
        payload = {key: value for key, value in document.items()
                   if key not in ('provenance', 'commissions', 'scalers')}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def _timestamp(value, name):
    if not isinstance(value, str):
        raise ValueError(f'{name} requires a timezone-aware ISO timestamp')
    try:
        result = datetime.fromisoformat(value)
        aware(result)
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError(f'{name} requires a timezone-aware ISO timestamp') from error
    return result


def _sha256(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


class FrozenParameterGuard:
    def __init__(self, document):
        self.artifacts = []
        if document.get('profile') == 'demo':
            return
        provenance = document.get('provenance')
        if not isinstance(provenance, dict):
            raise ValueError('non-demo frozen parameters require provenance')
        for group in PARAMETER_GROUPS:
            entry = deepcopy(provenance.get(group))
            name = f'provenance.{group}'
            if not isinstance(entry, dict) or not isinstance(entry.get('source'), str) or not entry['source'].strip():
                raise ValueError(f'{name}.source is required')
            if not _sha256(entry.get('code_hash')):
                raise ValueError(f'{name}.code_hash must be a lowercase SHA-256')
            trained = _timestamp(entry.get('trained_until'), name + '.trained_until')
            available = _timestamp(entry.get('available_at'), name + '.available_at')
            if trained >= available:
                raise ValueError(f'{name} training cutoff must precede artifact availability')
            if entry.get('content_sha256') != parameter_content_hash(document, group):
                raise ValueError(f'{name}.content_sha256 does not bind the frozen parameter values')
            expiry = _timestamp(entry['valid_until'], name + '.valid_until') if 'valid_until' in entry else None
            if expiry is not None and expiry <= available:
                raise ValueError(f'{name} expiry must follow availability')
            self.artifacts.append((name, available, expiry))

    def validate_as_of(self, at):
        aware(at)
        for name, available, expiry in self.artifacts:
            if at < available:
                raise ValueError(f'{name} frozen parameters are not yet available at receive time')
            if expiry is not None and at >= expiry:
                raise ValueError(f'{name} frozen parameter artifact has expired')
