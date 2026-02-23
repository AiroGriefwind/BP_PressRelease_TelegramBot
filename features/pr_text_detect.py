import re
from typing import Dict, List

import config


def _non_empty_lines(lines: List[str]) -> List[str]:
    return [ln for ln in lines if (ln or "").strip()]


def _normalize_marker_text(line: str) -> str:
    s = (line or "").strip()
    # 只做本功能需要的轻量繁化，保证识别关键词仍以繁体为主
    return (
        s.replace("新闻稿", "新聞稿")
        .replace("公关稿", "公關稿")
        .replace("新聞稿", "新聞稿")
        .replace("公關稿", "公關稿")
    )


def _is_news_marker_line(line: str) -> bool:
    if not line:
        return False
    cleaned = re.sub(r"[\s\*\-—_`~·•]+", "", _normalize_marker_text(line))
    return ("新聞稿" in cleaned) or ("公關稿" in cleaned)


def _extract_title_and_body(lines: List[str], marker_keyword: str) -> Dict[str, object]:
    non_empty_idx = [i for i, ln in enumerate(lines) if (ln or "").strip()]
    if not non_empty_idx:
        return {"header": "", "title": "", "title_lines": [], "body_lines": []}

    first_idx = non_empty_idx[0]
    header = ""
    start_idx = first_idx
    if _is_news_marker_line(lines[first_idx]):
        header = marker_keyword or _normalize_marker_text(lines[first_idx] or "").strip()
        if len(non_empty_idx) >= 2:
            start_idx = non_empty_idx[1]

    title_lines: List[str] = []
    body_start_idx = start_idx
    for i in range(start_idx, len(lines)):
        current = (lines[i] or "").strip()
        if not current:
            if title_lines:
                body_start_idx = i + 1
                break
            continue
        if _is_news_marker_line(current):
            continue

        # 标题通常较短，正文通常有句号且长度更长；用于判定分界
        looks_like_body = (
            len(current) >= config.PR_TEXT_BODY_LINE_MIN_CHARS
            and ("。" in current or "，" in current)
        )
        if title_lines and looks_like_body:
            body_start_idx = i
            break

        title_lines.append(current)
        if len(title_lines) >= config.PR_TEXT_MAX_TITLE_LINES:
            body_start_idx = i + 1
            break
        body_start_idx = i + 1

    title = title_lines[0] if title_lines else ""
    body_lines = [ln.rstrip() for ln in lines[body_start_idx:]]
    return {"header": header, "title": title, "title_lines": title_lines, "body_lines": body_lines}


def _detect_marker_keyword(lines: List[str]) -> str:
    for ln in lines[:3]:
        cleaned = re.sub(r"[\s\*\-—_`~·•]+", "", _normalize_marker_text(ln))
        if "公關稿" in cleaned:
            return "公關稿"
        if "新聞稿" in cleaned:
            return "新聞稿"
    return ""


def analyze_pr_text(text: str) -> Dict[str, object]:
    raw = text or ""
    lines = raw.splitlines()
    non_empty = _non_empty_lines(lines)
    compact_len = len(re.sub(r"\s+", "", raw))
    non_empty_count = len(non_empty)
    top_lines = non_empty[:3]

    marker_keyword = _detect_marker_keyword(top_lines)
    has_marker = bool(marker_keyword)
    is_long_enough = compact_len >= config.PR_TEXT_MIN_CHARS
    date_tail = any(
        re.search(r"(20\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日)", ln) for ln in non_empty[-4:]
    )
    has_org_kw = any(kw in raw for kw in config.PR_TEXT_ORG_KEYWORDS)

    # 仅按长度触发长文本处理，不再依赖标记或二次确认
    mode = "auto" if is_long_enough else "none"

    structured = _extract_title_and_body(lines, marker_keyword)
    return {
        "mode": mode,
        "is_long_enough": is_long_enough,
        "compact_len": compact_len,
        "non_empty_lines": non_empty_count,
        "has_marker": has_marker,
        "date_tail": date_tail,
        "has_org_kw": has_org_kw,
        "marker_keyword": marker_keyword,
        "header": structured["header"],
        "title": structured["title"],
        "title_lines": structured["title_lines"],
        "body_lines": structured["body_lines"],
    }
