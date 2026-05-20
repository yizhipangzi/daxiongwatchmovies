"""DeepL Japanese→Chinese translation, config-driven, idempotent.

The Free tier endpoint is ``api-free.deepl.com`` (keys ending in ``:fx``);
all other keys use ``api.deepl.com``. If no key is configured the module
returns ``None`` so callers can fall back to the original text.

Used by the briefing's 电影日和 section to translate eiga.com short reviews.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_DEEPL_FREE_URL = "https://api-free.deepl.com/v2/translate"
_DEEPL_PRO_URL = "https://api.deepl.com/v2/translate"

_config: dict = {}
_session: Optional[requests.Session] = None


def init_translator(cfg: dict) -> None:
    """Initialise with the top-level config dict (reads ``cfg['deepl']``)."""
    global _config
    _config = (cfg or {}).get("deepl") or {}


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


def _endpoint(api_key: str) -> str:
    return _DEEPL_FREE_URL if api_key.endswith(":fx") else _DEEPL_PRO_URL


def is_configured() -> bool:
    return bool((_config or {}).get("api_key"))


def translate_ja_to_zh(text: str, retries: int = 2) -> Optional[str]:
    """Translate Japanese ``text`` to Chinese. Returns translated string or None.

    None is returned when:
      - the input is empty/blank,
      - no DeepL key is configured (caller should fall back to the original),
      - or all retries fail.
    """
    if not text or not text.strip():
        return None
    api_key = (_config or {}).get("api_key", "")
    if not api_key:
        return None
    target = (_config or {}).get("target_lang") or "ZH"
    url = _endpoint(api_key)
    # DeepL deprecated form-body auth_key — use the Authorization header.
    headers = {"Authorization": f"DeepL-Auth-Key {api_key}"}
    payload = {
        "text": text,
        "source_lang": "JA",
        "target_lang": target,
    }
    sess = _get_session()
    for attempt in range(1, retries + 2):
        try:
            r = sess.post(url, data=payload, headers=headers, timeout=30)
            if r.status_code == 200:
                j = r.json()
                arr = j.get("translations") or []
                if arr:
                    out = arr[0].get("text") or ""
                    return out or None
                logger.warning("DeepL returned empty translations payload")
                return None
            if r.status_code == 429:
                logger.warning("DeepL 429 rate-limited, backing off (attempt %d)", attempt)
            elif r.status_code in (403, 456):
                # 456 = quota exceeded; 403 = bad key. Don't retry.
                logger.error("DeepL %d: %s", r.status_code, r.text[:200])
                return None
            else:
                logger.warning("DeepL %d: %s", r.status_code, r.text[:200])
        except Exception as exc:
            logger.warning("DeepL request failed (attempt %d): %s", attempt, exc)
        time.sleep(min(2 ** attempt, 8))
    return None
