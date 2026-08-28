"""Cross-check that the frontend ``dashboard`` i18n namespace has identical key sets across locales.

The frontend loads ``frontend/src/i18n/{zh,en,vi}/dashboard.ts`` as the ``dashboard`` namespace
(``frontend/src/i18n/index.ts``) and falls back to raw keys when one is missing. A key added in
one locale but not another silently renders as the raw key string in the other locales, so CI
catches the drift here instead of at render time. The backend error-key parity is covered by
``tests/unit/lib/i18n/test_i18n_consistency.py``; this file covers the frontend-only namespace.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_TS = "frontend/src/i18n/{locale}/dashboard.ts"
LOCALES = ("zh", "en", "vi")

_KEY_RE = re.compile(r"""['"]([a-z0-9_]+)['"]\s*:""")


def _load_keys(locale: str) -> set[str]:
    path = REPO_ROOT / DASHBOARD_TS.format(locale=locale)
    text = path.read_text(encoding="utf-8")
    return set(_KEY_RE.findall(text))


def test_dashboard_key_sets_identical_across_locales() -> None:
    key_sets = {locale: _load_keys(locale) for locale in LOCALES}
    base = key_sets[LOCALES[0]]
    for locale in LOCALES[1:]:
        missing = base - key_sets[locale]
        extra = key_sets[locale] - base
        assert not missing, f"{locale} dashboard 缺少 {LOCALES[0]} 已有的 key: {sorted(missing)}"
        assert not extra, f"{locale} dashboard 多了 {LOCALES[0]} 没有的 key: {sorted(extra)}"
