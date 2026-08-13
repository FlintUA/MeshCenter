#!/usr/bin/env python3
"""Check that all locale catalogs under static/i18n/ define the same keys.

Catalogs are nested-by-namespace JSON objects (e.g. {"time": {"card_title": "..."}}),
not flat dotted keys, so this recursively flattens each catalog into dotted key
paths before comparing them across languages.
"""
import json
import sys
from pathlib import Path

I18N_DIR = Path(__file__).parent.parent / 'static' / 'i18n'
LANGS = ['en', 'de', 'ru', 'uk']


def flatten(d, prefix=''):
    keys = set()
    for k, v in d.items():
        full = f'{prefix}.{k}' if prefix else k
        if isinstance(v, dict):
            keys |= flatten(v, full)
        else:
            keys.add(full)
    return keys


def load(lang):
    with open(I18N_DIR / f'{lang}.json', encoding='utf-8') as f:
        data = json.load(f)
    return flatten(data)


def main():
    catalogs = {lang: load(lang) for lang in LANGS}
    base = catalogs['en']
    ok = True
    for lang, keys in catalogs.items():
        missing = base - keys
        extra = keys - base
        if missing:
            print(f'{lang}: MISSING {len(missing)} keys: {sorted(missing)[:10]}')
            ok = False
        if extra:
            print(f'{lang}: EXTRA {len(extra)} keys: {sorted(extra)[:10]}')
            ok = False
    if ok:
        print('OK: all catalogs in sync')
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
