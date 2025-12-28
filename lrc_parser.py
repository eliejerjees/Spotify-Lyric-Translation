import re
from dataclasses import dataclass
from typing import List, Optional

@dataclass(frozen = True)
class LyricLine:
    t_ms: int
    original: str
    translated: Optional[str] = None

# LRC timestamps like [01:23.45] or [1:23.4] or [01:23]
_TS = re.compile(r"\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?\]")

def parse_lrc(lrc_text:str) -> List[LyricLine]:
    lines: List[LyricLine] = []

    for raw in lrc_text.splitlines():
        raw = raw.strip()
        if not raw:
            continue

        # Skip metadata tags like artists and title
        if raw.startswith("[") and ":" in raw and _TS.search(raw) is None:
            continue

        matches = list(_TS.finditer(raw))
        if not matches:
            continue

        # Text is after the last timestamp
        text = raw[matches[-1].end():].strip()
        if not text:
            continue

        for m in matches:
            mm = int(m.group(1))
            ss = int(m.group(2))
            frac = m.group(3)

            ms = 0
            if frac:
                # Convert fractional seconds to milliseconds
                if len(frac) == 1:
                    ms = int(frac) * 100
                elif len(frac) == 2:
                    ms = int(frac) * 10
                else:
                    ms = int(frac[:3])

            t_ms = (mm * 60 + ss) * 1000 + ms # Total milliseconds
            lines.append(LyricLine(t_ms=t_ms, original=text, translated=None))

    # Sort by time
    lines.sort(key=lambda x: x.t_ms)

    # Dedupe exact duplicates
    deduped: List[LyricLine] = []
    seen = set()
    for ln in lines:
        key = (ln.t_ms, ln.original)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(ln)

    return deduped
