# -*- coding: utf-8 -*-
"""1.0.4: очистка OCR-имён от ников/таймеров и выбор лучшей строки."""
import re

_NICK_DROP = re.compile(r"^[A-Za-zА-Яа-яЁё0-9_\-.]{3,}$")
_TIME_RE = re.compile(r"\b\d{1,2}\s*[чh]\s*\d{1,2}\s*[мm]?\b",
                      re.IGNORECASE)
_LONG_DIGITS = re.compile(r"\b\d{5,}\b")
_JUNK_WORDS = ("совпедать", "совпадать", "не жопт", "1ч", "1m")
_SERVICE_RE = re.compile(
    r"(\b1?\s*В\s*НАЛИЧИИ|\b\d+\s*в\s*наличии|"
    r"Создано\s*[-−]?\s*\d+|компл\.?\s*\d{0,4}|онлайн|"
    r"[✓✔×x]\s*\d{1,4})", re.IGNORECASE)


def _norm(text):
    return re.sub(r"[_\W]+", "", str(text or "").casefold()
                  .replace("ё", "е"))


def _nick_set(nicknames):
    return {_norm(n) for n in (nicknames or []) if _norm(n)}


def has_timecode(text):
    low = str(text or "").lower()
    return bool(_TIME_RE.search(low)) or any(
        marker in low for marker in _JUNK_WORDS)


def clean_card_name(text, nicknames=()):
    """Убрать ники, длинные цифровые хвосты и таймкоды из OCR-строки."""
    raw = str(text or "")
    if not raw:
        return ""
    raw = _SERVICE_RE.sub(" ", raw)
    raw = _TIME_RE.sub(" ", raw)
    raw = _LONG_DIGITS.sub(" ", raw)
    nicks = _nick_set(nicknames)
    tokens = raw.split()
    kept = []
    for token in tokens:
        norm = _norm(token)
        if norm and norm in nicks:
            continue
        if token.isdigit() and len(token) >= 5:
            continue
        kept.append(token)
    text = " ".join(kept)
    text = re.sub(r"\s+", " ", text).strip(" -:;,")
    return text


def is_valid_card_text(text):
    """Отсеять чат/таймеры/мусорные строки."""
    raw = str(text or "").strip()
    if not raw:
        return False
    if has_timecode(raw):
        return False
    if len(raw) < 3:
        return False
    # нужна хотя бы одна буква
    if not re.search(r"[A-Za-zА-Яа-яЁё]", raw):
        return False
    return True


def best_catalog_line(text, matcher, nicknames=(), threshold=0.6):
    """Проверить строки по отдельности, вернуть (name, score, raw_line)."""
    lines = [line.strip() for line in str(text or "").splitlines()
             if line.strip()]
    best = ("", 0.0, "")
    for line in lines:
        cleaned = clean_card_name(line, nicknames)
        if not is_valid_card_text(cleaned):
            continue
        try:
            score = float(matcher(cleaned) or 0.0)
        except Exception:
            score = 0.0
        if score > best[1]:
            best = (cleaned, score, line)
    if best[1] >= threshold:
        return best
    # ни одна строка не дала матч — пробуем склеенный вариант
    joined = clean_card_name(" ".join(lines), nicknames)
    if is_valid_card_text(joined):
        try:
            score = float(matcher(joined) or 0.0)
        except Exception:
            score = 0.0
        if score > best[1]:
            best = (joined, score, joined)
    return best


def is_card_frame(width, height=0, text_lines=0, scale=1.0):
    """Рамка похожа на карточку: 200…260 px ширины при 1080p, 2+ строки."""
    try:
        width = float(width) / max(0.1, float(scale))
    except Exception:
        return False
    if not (190.0 <= width <= 270.0):
        return False
    return int(text_lines or 0) >= 2


def choose_row(rows, matcher, threshold=0.6):
    """Выбрать ряд с максимумом совпадений; rows=[{name,lefts,texts}]."""
    best = None
    for row in rows or []:
        texts = row.get("texts") or []
        matches = 0
        for text in texts:
            cleaned = clean_card_name(text)
            if not is_valid_card_text(cleaned):
                continue
            try:
                if float(matcher(cleaned) or 0.0) >= threshold:
                    matches += 1
            except Exception:
                pass
        item = dict(row)
        item["matches"] = matches
        if best is None or matches > best.get("matches", 0):
            best = item
    return best


def row_log_line(row, total=0):
    if not row:
        return "Ряд: не найден"
    lefts = ",".join(str(int(round(float(x))))
                     for x in (row.get("lefts") or []))
    return (f"Ряд: {row.get('name') or '?'} (lefts=[{lefts}]) — "
            f"{row.get('matches', 0)}/{total or len(row.get('texts') or [])} "
            "совпадений, выбран")


__all__ = ["clean_card_name", "is_valid_card_text", "has_timecode",
           "is_card_frame", "choose_row", "row_log_line",
           "best_catalog_line"]
