from bisect import bisect_right
from typing import List, Optional

def current_line_index(t_ms_list: List[int], progress_ms: Optional[int]) -> int:
    """
    Returns the index i such that t_ms_list[i] <= progress_ms < t_ms_list[i+1].
    If progress_ms is before the first timestamp, returns -1 (no line yet).
    If progress_ms is after the last timestamp, returns last index.
    """
    if progress_ms is None or not t_ms_list:
        return -1

    # bisect_right gives insertion point to keep list sorted,
    # so subtract 1 to get the last timestamp <= progress
    i = bisect_right(t_ms_list, progress_ms) - 1
    return i

def window(lines: List[dict], idx: int, before: int = 2, after: int = 6) -> List[dict]:
    """
    Returns a slice around idx for UI: [idx-before, idx+after].
    """
    if not lines:
        return []
    start = max(0, idx - before)
    end = min(len(lines), idx + after + 1)
    return lines[start:end]
