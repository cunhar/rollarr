"""
services/release_selector.py
----------------------------
Sonarr/Radarr-inspired NZB release title parser, junk filter, and scoring engine.

Supports:
  - Preferred Resolution ('2160p', '1080p', '720p', 'any')
  - Preferred Audio ('any', 'atmos', 'dts', 'ddp', 'aac')
  - Strict Junk Filter (CAM/TS/HC/SAMPLE/3D/passworded/wrong episode/size sanity)
  - Source tier bonus (BluRay/WEB-DL > Remux/WEBRip > HDTV)
  - PROPER / REPACK bonus & grabs tiebreaker
  - Blocklist exclusion (failed NZB GUIDs)
"""
from __future__ import annotations

import re
import math
import logging
from typing import Any

from config_store import get_config
from integrations.common import clean_title

logger = logging.getLogger(__name__)

# ── Regex Patterns ───────────────────────────────────────────────────────────

_RE_RESOLUTION = re.compile(r'\b(2160p|4k|uhd|1080p|720p|480p|576p)\b', re.I)

_RE_SOURCE = [
    ('remux',  re.compile(r'\bremux\b', re.I)),
    ('bluray', re.compile(r'\b(blu[\-\.\s]?ray|bdrip|brrip)\b', re.I)),
    ('webdl',  re.compile(r'\b(web[\-\.\s]?dl|webdl|amzn|nf|dsnp|atvp|hmax|pcok)\b', re.I)),
    ('webrip', re.compile(r'\b(web[\-\.\s]?rip|webrip|\bweb\b)\b', re.I)),
    ('hdtv',   re.compile(r'\b(hdtv|pdtv|dsr)\b', re.I)),
]

_RE_AUDIO = [
    ('atmos', re.compile(r'\b(atmos|truehd|true[\-\.\s]?hd)\b', re.I)),
    ('dts',   re.compile(r'\b(dts[\-\.\s]?hd|dts[\-\.\s]?x|dts[\-\.\s]?ma|\bdts\b)\b', re.I)),
    ('ddp',   re.compile(r'\b(ddp\d*(?:\.\d)?|dd\+|e[\-\.\s]?ac[\-\.\s]?3|eac3|dd\d[\.\s]?\d|ac3|dolby[\-\.\s]?digital)\b', re.I)),
    ('aac',   re.compile(r'\b(aac\d*(?:\.\d)?|flac|opus|mp3)\b', re.I)),
]

_RE_CODEC = [
    ('av1',  re.compile(r'\bav1\b', re.I)),
    ('x265', re.compile(r'\b(x265|h[\.\s]?265|hevc)\b', re.I)),
    ('x264', re.compile(r'\b(x264|h[\.\s]?264|avc)\b', re.I)),
]

_RE_PROPER = re.compile(r'\b(proper|repack|rerip|real[\.\s]proper)\b', re.I)

_RE_JUNK = re.compile(
    r'\b('
    r'cam|hdcam|camrip|'
    r'ts|hdts|telesync|pdvd|'
    r'tc|telecine|'
    r'wp|workprint|'
    r'scr|screener|dvdscr|bdscr|'
    r'r5|r6|'
    r'sample|trailer|extras|bonus|'
    r'hc|hardcoded|korsub|kor[\.\s]?sub|hindi[\.\s]?sub|'
    r'3d|hsbs|sbs|hou|half[\-\.\s]?sbs|half[\-\.\s]?ou|'
    r'xxx|porn'
    r')\b',
    re.I,
)

# Episode markers: S01E02, S01E02E03, 1x02
_RE_EPISODE = re.compile(r'(?:^|[\.\s_\-])S(\d{1,2})E(\d{1,3})(?:[\-E](\d{1,3}))?(?:$|[\.\s_\-])', re.I)
_RE_EPISODE_ALT = re.compile(r'(?:^|[\.\s_\-])(\d{1,2})x(\d{1,3})(?:$|[\.\s_\-])', re.I)
# Season pack marker (e.g. S01 without E01, or "Season 1", "Complete")
_RE_SEASON_ONLY = re.compile(r'(?:^|[\.\s_\-])(?:S(\d{1,2})(?!E\d)|Season[\.\s_\-]?(\d{1,2})|Complete[\.\s_\-]?Series)(?:$|[\.\s_\-])', re.I)
_RE_YEAR = re.compile(r'(?:^|[\.\s_\-\(\[])((?:19|20)\d{2})(?:$|[\.\s_\-\)\]])')

RES_TIERS = ['480p', '720p', '1080p', '2160p']


def parse_release(title: str) -> dict[str, Any]:
    """Parse quality, audio, source, codec, and episode/year metadata from a release title."""
    t = title or ''

    res_m = _RE_RESOLUTION.search(t)
    resolution = 'unknown'
    if res_m:
        raw_res = res_m.group(1).lower()
        if raw_res in ('4k', 'uhd', '2160p'):
            resolution = '2160p'
        elif raw_res in ('480p', '576p'):
            resolution = '480p'
        else:
            resolution = raw_res

    source = 'unknown'
    for src_name, pat in _RE_SOURCE:
        if pat.search(t):
            source = src_name
            break

    audio = 'unknown'
    for aud_name, pat in _RE_AUDIO:
        if pat.search(t):
            audio = aud_name
            break

    codec = 'unknown'
    for cod_name, pat in _RE_CODEC:
        if pat.search(t):
            codec = cod_name
            break

    season = None
    episode = None
    ep_end = None
    is_season_pack = False

    ep_m = _RE_EPISODE.search(t)
    if ep_m:
        season = int(ep_m.group(1))
        episode = int(ep_m.group(2))
        if ep_m.group(3):
            ep_end = int(ep_m.group(3))
    else:
        alt_m = _RE_EPISODE_ALT.search(t)
        if alt_m:
            season = int(alt_m.group(1))
            episode = int(alt_m.group(2))
        elif _RE_SEASON_ONLY.search(t):
            is_season_pack = True

    years = [int(y) for y in _RE_YEAR.findall(t)]
    year = years[-1] if years else None

    return {
        'resolution': resolution,
        'source': source,
        'audio': audio,
        'codec': codec,
        'is_proper': bool(_RE_PROPER.search(t)),
        'is_junk_tag': bool(_RE_JUNK.search(t)),
        'season': season,
        'episode': episode,
        'ep_end': ep_end,
        'is_season_pack': is_season_pack,
        'year': year,
    }


def _title_matches(release_title: str, expected_title: str) -> bool:
    """Verify the release title actually contains the expected show/movie title words."""
    if not expected_title:
        return True
    rel_clean = clean_title(release_title)
    exp_clean = clean_title(expected_title)
    if not exp_clean:
        return True
    if exp_clean in rel_clean:
        return True
    # Fallback: check significant words (len >= 3)
    words = [w for w in re.findall(r'[a-z0-9]+', expected_title.lower()) if len(w) >= 3]
    if not words:
        return True
    matched = sum(1 for w in words if w in rel_clean)
    return matched >= max(1, math.ceil(len(words) * 0.7))


def is_junk_release(
    candidate: dict[str, Any],
    parsed: dict[str, Any],
    media_type: str,
    expected: dict[str, Any],
    blocklisted_guids: set[str],
    max_size_gb: float,
) -> tuple[bool, str]:
    """Return (True, reason) if the candidate should be hard-rejected."""
    guid = candidate.get('guid') or ''
    if guid and guid in blocklisted_guids:
        return True, 'blocklisted'

    if candidate.get('passworded'):
        return True, 'passworded'

    if parsed['is_junk_tag']:
        return True, 'junk_tag'

    size_bytes = int(candidate.get('size_bytes') or 0)
    min_mb = 80 if media_type == 'episode' else 300
    if size_bytes > 0:
        if size_bytes < min_mb * 1024 * 1024:
            return True, f'too_small ({size_bytes // (1024*1024)} MB)'
        if max_size_gb > 0 and size_bytes > max_size_gb * (1024 ** 3):
            return True, f'too_large ({size_bytes / (1024**3):.1f} GB)'

    rel_title = candidate.get('title') or ''
    exp_title = expected.get('title') or ''

    if media_type == 'episode':
        if parsed['is_season_pack']:
            return True, 'season_pack'
        exp_s = expected.get('season')
        exp_e = expected.get('episode')
        if parsed['season'] is not None and exp_s is not None and parsed['season'] != int(exp_s):
            return True, f"wrong_season (S{parsed['season']:02d})"
        if parsed['episode'] is not None and exp_e is not None:
            ep_ok = (parsed['episode'] == int(exp_e)) or (
                parsed['ep_end'] is not None and parsed['episode'] <= int(exp_e) <= parsed['ep_end']
            )
            if not ep_ok:
                return True, f"wrong_episode (E{parsed['episode']:02d})"
        if not _title_matches(rel_title, exp_title):
            return True, 'title_mismatch'
    else:
        # Movie checks
        if parsed['season'] is not None or parsed['is_season_pack']:
            return True, 'tv_release_for_movie'
        exp_year = expected.get('year')
        if exp_year and parsed['year'] and abs(int(exp_year) - parsed['year']) > 1:
            return True, f"wrong_year ({parsed['year']} != {exp_year})"
        if not _title_matches(rel_title, exp_title):
            return True, 'title_mismatch'

    return False, ''


def score_release(
    candidate: dict[str, Any],
    parsed: dict[str, Any],
    preferred_resolution: str = '1080p',
    preferred_audio: str = 'any',
) -> float:
    """Compute a Sonarr/Radarr-inspired quality score for a non-junk release."""
    score = 0.0
    pref_res = (preferred_resolution or '1080p').lower()
    pref_aud = (preferred_audio or 'any').lower()

    # 1. Resolution match
    res = parsed['resolution']
    if pref_res == 'any':
        score += {'1080p': 50, '2160p': 45, '720p': 35, '480p': 15}.get(res, 10)
    elif res == pref_res:
        score += 100
    elif res in RES_TIERS and pref_res in RES_TIERS:
        diff = abs(RES_TIERS.index(res) - RES_TIERS.index(pref_res))
        if diff == 1:
            # Prefer stepping down slightly over stepping up unexpectedly
            score += 45 if RES_TIERS.index(res) < RES_TIERS.index(pref_res) else 35
        else:
            score += 10
    else:
        score += 5

    # 2. Audio match
    aud = parsed['audio']
    if pref_aud == 'any':
        score += {'atmos': 20, 'dts': 18, 'ddp': 16, 'aac': 12}.get(aud, 5)
    elif aud == pref_aud:
        score += 60
    else:
        # Partial credit for high-tier surround if user prefers Atmos/DTS/DDP
        if pref_aud in ('atmos', 'dts', 'ddp') and aud in ('atmos', 'dts', 'ddp'):
            score += 25
        elif aud != 'unknown':
            score += 10

    # 3. Source quality tier
    score += {
        'bluray': 30,
        'webdl':  30,
        'remux':  25,
        'webrip': 20,
        'hdtv':   10,
    }.get(parsed['source'], 5)

    # 4. PROPER / REPACK reliability bonus (like Sonarr/Radarr)
    if parsed['is_proper']:
        score += 15

    # 5. Popularity / grabs tiebreaker (capped at +10)
    grabs = int(candidate.get('grabs') or 0)
    if grabs > 0:
        score += min(10.0, math.log10(grabs + 1) * 4.0)

    return round(score, 2)


def select_releases(
    candidates: list[dict[str, Any]],
    media_type: str,
    expected: dict[str, Any],
    blocklisted_guids: set[str] | None = None,
    preferred_resolution: str | None = None,
    preferred_audio: str | None = None,
    max_size_gb: float | None = None,
) -> list[dict[str, Any]]:
    """
    Filter out junk/blocklisted releases and return remaining candidates sorted
    by descending score, each annotated with `parsed` and `score`.
    """
    pref_res = preferred_resolution or get_config('PREFERRED_RESOLUTION', '1080p')
    pref_aud = preferred_audio or get_config('PREFERRED_AUDIO', 'any')
    if max_size_gb is None:
        key = 'MAX_EPISODE_SIZE_GB' if media_type == 'episode' else 'MAX_MOVIE_SIZE_GB'
        default_max = 8 if media_type == 'episode' else 25
        max_size_gb = float(get_config(key, default_max))

    blocked = blocklisted_guids or set()
    accepted = []

    for cand in candidates:
        parsed = parse_release(cand.get('title', ''))
        junk, reason = is_junk_release(cand, parsed, media_type, expected, blocked, max_size_gb)
        if junk:
            logger.debug(f"[ReleaseSelector] Rejected '{cand.get('title')}' ({reason})")
            continue
        sc = score_release(cand, parsed, pref_res, pref_aud)
        accepted.append({
            **cand,
            'parsed': parsed,
            'score': sc,
        })

    accepted.sort(key=lambda r: (r['score'], r.get('grabs', 0)), reverse=True)
    return accepted
