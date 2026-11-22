"""
services/subtitle_service.py
----------------------------
Keyless subtitle auto-downloader using the public OpenSubtitles v3 endpoint
(opensubtitles-v3.strem.io), supporting video-hash matching for accurate sync.

Defaults to English ('en') and Portuguese ('pt', preferring European 'por'
with Brazilian 'pob' fallback), configurable via SUBTITLES_ENABLED and
SUBTITLE_LANGUAGES in config_store.
"""
from __future__ import annotations

import os
import struct
import logging
from typing import Any

import requests

from config_store import get_config

logger = logging.getLogger(__name__)

OS_V3_BASE = 'https://opensubtitles-v3.strem.io/subtitles'

# Maps user-friendly language codes to ordered OpenSubtitles 3-letter codes
LANG_MAP: dict[str, list[str]] = {
    'en':    ['eng'],
    'eng':   ['eng'],
    'pt':    ['por', 'pob'],
    'por':   ['por', 'pob'],
    'pt-pt': ['por'],
    'pt-br': ['pob'],
    'pob':   ['pob'],
    'es':    ['spa'],
    'spa':   ['spa'],
    'fr':    ['fre', 'fra'],
    'de':    ['ger', 'deu'],
    'it':    ['ita'],
    'nl':    ['dut', 'nld'],
    'pl':    ['pol'],
    'ru':    ['rus'],
    'sv':    ['swe'],
    'tr':    ['tur'],
    'ar':    ['ara'],
    'ja':    ['jpn'],
    'ko':    ['kor'],
    'zh':    ['chi', 'zho'],
}


def parse_subtitle_languages(raw: str | None = None) -> list[str]:
    """Parse comma-separated language codes from config (default: ['en', 'pt'])."""
    val = raw if raw is not None else (get_config('SUBTITLE_LANGUAGES', 'en, pt') or '')
    langs: list[str] = []
    for part in str(val).split(','):
        code = part.strip().lower()
        if code and code not in langs:
            langs.append(code)
    return langs


def _compute_os_hash(file_path: str) -> tuple[str | None, int]:
    """Compute standard OpenSubtitles 64-bit hash and file size."""
    try:
        file_size = os.path.getsize(file_path)
        if file_size < 65536 * 2:
            return None, file_size
        longlongformat = '<q'
        bytesize = struct.calcsize(longlongformat)
        h = file_size
        with open(file_path, 'rb') as f:
            for _ in range(65536 // bytesize):
                (val,) = struct.unpack(longlongformat, f.read(bytesize))
                h = (h + val) & 0xFFFFFFFFFFFFFFFF
            f.seek(max(0, file_size - 65536), 0)
            for _ in range(65536 // bytesize):
                (val,) = struct.unpack(longlongformat, f.read(bytesize))
                h = (h + val) & 0xFFFFFFFFFFFFFFFF
        return f"{h:016x}", file_size
    except Exception as exc:
        logger.debug(f"[Subtitles] Hash calculation failed for {file_path}: {exc}")
        return None, 0


def _pick_best_sub(subtitles: list[dict[str, Any]], target_lang: str) -> dict[str, Any] | None:
    """Pick the first subtitle matching target_lang in preferred priority order (e.g. por before pob)."""
    candidates_codes = LANG_MAP.get(target_lang, [target_lang])
    for code in candidates_codes:
        for sub in subtitles:
            if (sub.get('lang') or '').lower() == code and sub.get('url'):
                return sub
    return None


def download_subtitles_for_media(
    video_path: str,
    media_type: str,
    imdb_id: str | None,
    season: int | None = None,
    episode: int | None = None,
    languages: list[str] | None = None,
) -> list[str]:
    """
    Download matching .srt subtitles next to `video_path` for each configured language.
    Returns list of language codes successfully downloaded.
    """
    if not bool(get_config('SUBTITLES_ENABLED', True)):
        return []
    if not imdb_id or not video_path or not os.path.isfile(video_path):
        return []

    langs = languages if languages is not None else parse_subtitle_languages()
    if not langs:
        return []

    clean_imdb = str(imdb_id).strip()
    if not clean_imdb.startswith('tt'):
        clean_imdb = f"tt{clean_imdb}"

    if media_type == 'episode' and season is not None and episode is not None:
        media_id = f"{clean_imdb}:{int(season)}:{int(episode)}"
        kind = 'series'
    else:
        media_id = clean_imdb
        kind = 'movie'

    os_hash, file_size = _compute_os_hash(video_path)
    if os_hash and file_size > 0:
        url = f"{OS_V3_BASE}/{kind}/{media_id}/videoHash={os_hash}&videoSize={file_size}.json"
    else:
        url = f"{OS_V3_BASE}/{kind}/{media_id}.json"

    try:
        resp = requests.get(url, headers={'User-Agent': 'Rolarr/1.0'}, timeout=12)
        resp.raise_for_status()
        subtitles = resp.json().get('subtitles') or []
    except Exception as exc:
        logger.warning(f"[Subtitles] Lookup failed for {media_id}: {exc}")
        return []

    if not subtitles:
        logger.info(f"[Subtitles] No subtitles found for {media_id}")
        return []

    stem = os.path.splitext(video_path)[0]
    downloaded: list[str] = []

    for lang in langs:
        out_path = f"{stem}.{lang}.srt"
        if os.path.exists(out_path):
            continue
        match = _pick_best_sub(subtitles, lang)
        if not match:
            continue
        try:
            s_resp = requests.get(match['url'], headers={'User-Agent': 'Rolarr/1.0'}, timeout=15)
            s_resp.raise_for_status()
            content = s_resp.content
            if len(content) < 50:
                continue
            with open(out_path, 'wb') as fh:
                fh.write(content)
            downloaded.append(lang)
            logger.info(f"[Subtitles] Saved {os.path.basename(out_path)} ({match.get('lang')})")
        except Exception as exc:
            logger.warning(f"[Subtitles] Failed downloading {lang} subtitle: {exc}")

    return downloaded
