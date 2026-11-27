# -*- coding: utf-8 -*-
"""Единственная точка открытия внешних ссылок.

В тестах и в гейте браузер не открывается: `WF_NO_BROWSER=1`.
Без этого флага GUI-клики по «Документация»/«Открыть на сайте» открывали
реальные окна браузера при каждом прогоне тестов.
"""
import os
import webbrowser


def open_url(url):
    """Открыть ссылку в системном браузере, если это разрешено."""
    url = str(url or "").strip()
    if not url or os.environ.get("WF_NO_BROWSER"):
        return False
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False
