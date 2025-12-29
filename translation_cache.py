import time
from typing import Dict, Tuple, List, Optional

# key: (track_id, lang)
# value: {"created_at": int, "lines": [{"t": int, "orig": str, "trans": str}, ...]}
_CACHE: Dict[Tuple[str, str], dict] = {}

def get_cached(track_id: str, lang: str, max_age_seconds: int = 6 * 60 * 60) -> Optional[List[dict]]:
    item = _CACHE.get((track_id, lang))
    if not item:
        return None
    if int(time.time()) - item["created_at"] > max_age_seconds:
        _CACHE.pop((track_id, lang), None)
        return None
    return item["lines"]

def set_cached(track_id: str, lang: str, lines: List[dict]) -> None:
    _CACHE[(track_id, lang)] = {"created_at": int(time.time()), "lines": lines}
