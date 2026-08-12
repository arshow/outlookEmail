"""Knowledge base matching helpers."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Sequence


def parse_keywords(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip().lower() for item in value if str(item).strip()]
    text = str(value or '').strip()
    if not text:
        return []
    if text.startswith('['):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(item).strip().lower() for item in parsed if str(item).strip()]
        except Exception:
            pass
    return [part.strip().lower() for part in text.replace('，', ',').split(',') if part.strip()]


def match_knowledge_entries(
    entries: Sequence[Dict[str, Any]],
    haystack: str,
    *,
    limit: int = 6,
) -> List[Dict[str, Any]]:
    text = str(haystack or '').lower()
    scored: List[tuple] = []
    for entry in entries:
        if not entry.get('enabled', True):
            continue
        keywords = parse_keywords(entry.get('keywords'))
        score = 0
        if keywords:
            score = sum(1 for keyword in keywords if keyword and keyword in text)
            if score <= 0:
                continue
        else:
            # No keywords: keep as low-priority general fact.
            score = 0
        priority = int(entry.get('priority') or 0)
        scored.append((score, priority, entry))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    if not scored:
        # Fall back to top priority general entries when nothing keyword-matched.
        general = sorted(
            [e for e in entries if e.get('enabled', True)],
            key=lambda e: int(e.get('priority') or 0),
            reverse=True,
        )
        return list(general[: min(3, limit)])
    return [item[2] for item in scored[:limit]]
