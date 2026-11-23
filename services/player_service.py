"""
services/player_service.py
--------------------------
Built-in HTML5 Video Player & Smart Remux/Transcode Streamer for Rolarr.
Designed for LG webOS TV Browser, desktop, and mobile browsers:
  - Native HTTP 206 byte-range streaming for MP4 files with AAC audio
  - Zero-CPU video copy (-c:v copy) + stereo AAC audio remuxing to fragmented MP4
    for MKV containers and AC3/EAC3/DTS/TrueHD audio tracks
  - On-the-fly SRT -> WebVTT subtitle conversion for sidecar (.en.srt, .pt.srt)
    and embedded MKV subtitle tracks
  - Active stream session tracking (integrates with Dashboard & Shutdown Watcher)
  - Automatic watched-file recycling & rolling-window queueing at >= 90% progress
"""
from __future__ import annotations

import os
import re
import json
import time
import shutil
import threading
import subprocess
import logging
from typing import Any, Generator

from flask import Response, request

from config_store import get_config, save_config
from integrations.common import is_safe_subpath
from integrations import metadata
from services import db, media_manager

logger = logging.getLogger(__name__)

_sessions_lock = threading.Lock()
_active_sessions: dict[str, dict[str, Any]] = {}
SESSION_TTL_SEC = 45

_log_callback = None

LANG_NAMES = {
    'en': 'English',
    'eng': 'English',
    'pt': 'Portuguese',
    'por': 'Portuguese (PT)',
    'pob': 'Portuguese (BR)',
    'pt-pt': 'Portuguese (PT)',
    'pt-br': 'Portuguese (BR)',
    'es': 'Spanish',
    'spa': 'Spanish',
    'fr': 'French',
    'fre': 'French',
    'fra': 'French',
    'de': 'German',
    'ger': 'German',
    'deu': 'German',
    'it': 'Italian',
    'ita': 'Italian',
    'nl': 'Dutch',
    'dut': 'Dutch',
    'nld': 'Dutch',
    'pl': 'Polish',
    'pol': 'Polish',
    'ru': 'Russian',
    'rus': 'Russian',
    'ja': 'Japanese',
    'jpn': 'Japanese',
    'ko': 'Korean',
    'kor': 'Korean',
    'zh': 'Chinese',
    'chi': 'Chinese',
    'zho': 'Chinese',
    'ar': 'Arabic',
    'ara': 'Arabic',
    'tr': 'Turkish',
    'tur': 'Turkish',
    'sv': 'Swedish',
    'swe': 'Swedish',
    'no': 'Norwegian',
    'nor': 'Norwegian',
    'da': 'Danish',
    'dan': 'Danish',
    'fi': 'Finnish',
    'fin': 'Finnish',
    'hi': 'Hindi',
    'hin': 'Hindi',
    'und': 'Default',
}

COPYABLE_VIDEO_CODECS = {'h264', 'avc', 'avc1', 'hevc', 'h265', 'hev1', 'hvc1'}
NATIVE_AUDIO_CODECS = {'aac', 'mp3', 'opus'}
TEXT_SUBTITLE_CODECS = {'subrip', 'srt', 'ass', 'ssa', 'webvtt', 'mov_text', 'text'}


def set_log_callback(fn):
    global _log_callback
    _log_callback = fn


def _activity_log(status: str, message: str, payload: dict | None = None):
    logger.info(f"[Player] {message}")
    if _log_callback:
        try:
            _log_callback(status, message, payload)
        except Exception as exc:
            logger.debug(f"[Player] Activity log callback error: {exc}")


def has_ffmpeg() -> bool:
    return shutil.which('ffmpeg') is not None


def has_ffprobe() -> bool:
    return shutil.which('ffprobe') is not None


# ── File Resolution & Safety ─────────────────────────────────────────────────

def resolve_media_file(
    media_type: str,
    media_id: int,
    season: int | None = None,
    episode: int | None = None,
) -> tuple[str | None, dict[str, Any] | None, str | None]:
    """
    Resolve a tracked movie or TV episode to its verified on-disk file path.
    Returns (file_path, media_record, error_message).
    """
    media_type = (media_type or '').lower()
    if media_type == 'movie':
        movie = db.get_movie(int(media_id))
        if not movie:
            return None, None, 'Movie not found in library'
        movies_root = get_config('PATH_MOVIES', '/movies') or '/movies'
        fpath = movie.get('file_path')
        if not fpath or not os.path.isfile(fpath) or not is_safe_subpath(fpath, movies_root):
            fpath = media_manager.find_movie_file_on_disk(movie['title'], movie.get('year'))
        if not fpath or not os.path.isfile(fpath):
            return None, movie, 'Movie file not found on disk'
        if not is_safe_subpath(fpath, movies_root):
            return None, movie, 'Unsafe movie file path'
        return fpath, movie, None

    elif media_type == 'episode':
        show = db.get_show(int(media_id))
        if not show:
            return None, None, 'TV show not found in library'
        if season is None or episode is None:
            return None, show, 'Season and episode numbers are required'
        tv_root = get_config('PATH_TV', '/tv') or '/tv'
        fpath = media_manager.find_episode_file_on_disk(show['title'], int(season), int(episode))
        if not fpath or not os.path.isfile(fpath):
            return None, show, f"Episode S{int(season):02d}E{int(episode):02d} file not found on disk"
        if not is_safe_subpath(fpath, tv_root):
            return None, show, 'Unsafe episode file path'
        return fpath, show, None

    return None, None, 'Invalid media type'


# ── FFprobe Media Inspection ─────────────────────────────────────────────────

def _parse_tag_duration(dur_str: str | None) -> float:
    """Parse Matroska tag DURATION like '00:54:14.160000000' into seconds."""
    if not dur_str:
        return 0.0
    m = re.match(r'^(\d+):(\d{2}):(\d{2}(?:\.\d+)?)', str(dur_str).strip())
    if not m:
        return 0.0
    try:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except (ValueError, TypeError):
        return 0.0


def probe_media_file(file_path: str) -> dict[str, Any]:
    """
    Inspect container, duration, video codec, audio streams, and embedded text subtitle streams.
    Falls back gracefully if ffprobe is not installed.
    """
    ext = os.path.splitext(file_path)[1].lower()
    default_info: dict[str, Any] = {
        'container': ext.lstrip('.'),
        'duration_sec': 0.0,
        'video_codec': 'h264',
        'audio_tracks': [{'index': 0, 'stream_index': 1, 'codec': 'aac' if ext == '.mp4' else 'ac3', 'lang': 'und', 'label': 'Default Audio', 'channels': 2}],
        'embedded_subtitles': [],
        'can_direct_play': ext == '.mp4',
    }

    if not has_ffprobe():
        return default_info

    try:
        cmd = [
            'ffprobe', '-v', 'quiet',
            '-print_format', 'json',
            '-show_format', '-show_streams',
            file_path,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if proc.returncode != 0 or not proc.stdout:
            return default_info

        data = json.loads(proc.stdout)
        fmt = data.get('format') or {}
        streams = data.get('streams') or []

        duration_sec = float(fmt.get('duration') or 0.0)
        video_codec = 'h264'
        audio_tracks: list[dict[str, Any]] = []
        embedded_subs: list[dict[str, Any]] = []

        audio_ord = 0
        sub_ord = 0
        for st in streams:
            ctype = (st.get('codec_type') or '').lower()
            cname = (st.get('codec_name') or '').lower()
            tags = st.get('tags') or {}
            lang = (tags.get('language') or tags.get('LANGUAGE') or 'und').lower()
            title = tags.get('title') or tags.get('TITLE') or ''

            if ctype == 'video' and video_codec == 'h264':
                video_codec = cname or 'h264'
                if not duration_sec and st.get('duration'):
                    try:
                        duration_sec = float(st['duration'])
                    except (ValueError, TypeError):
                        pass
                if not duration_sec:
                    duration_sec = _parse_tag_duration(tags.get('DURATION') or tags.get('duration'))

            elif ctype == 'audio':
                ch = int(st.get('channels') or 2)
                ch_str = '5.1' if ch == 6 else ('7.1' if ch == 8 else f'{ch}ch')
                lang_label = LANG_NAMES.get(lang, lang.upper())
                label_parts = [lang_label, cname.upper(), ch_str]
                if title and title.lower() not in lang_label.lower():
                    label_parts.append(f"({title})")
                audio_tracks.append({
                    'index': audio_ord,
                    'stream_index': int(st.get('index', audio_ord)),
                    'codec': cname,
                    'lang': lang,
                    'channels': ch,
                    'label': ' · '.join(label_parts),
                })
                audio_ord += 1

            elif ctype == 'subtitle' and cname in TEXT_SUBTITLE_CODECS:
                lang_label = LANG_NAMES.get(lang, lang.upper())
                sub_label = f"{lang_label} ({title})" if title else f"{lang_label} (Embedded)"
                embedded_subs.append({
                    'id': f"emb_{sub_ord}",
                    'sub_index': sub_ord,
                    'stream_index': int(st.get('index', sub_ord)),
                    'codec': cname,
                    'lang': lang,
                    'label': sub_label,
                    'source': 'embedded',
                })
                sub_ord += 1

        if not audio_tracks:
            audio_tracks = default_info['audio_tracks']

        first_audio_codec = audio_tracks[0]['codec'] if audio_tracks else 'aac'
        can_direct_play = (
            ext == '.mp4'
            and video_codec in COPYABLE_VIDEO_CODECS
            and first_audio_codec in NATIVE_AUDIO_CODECS
        )

        return {
            'container': ext.lstrip('.'),
            'duration_sec': round(duration_sec, 2),
            'video_codec': video_codec,
            'audio_tracks': audio_tracks,
            'embedded_subtitles': embedded_subs,
            'can_direct_play': can_direct_play,
        }
    except Exception as exc:
        logger.warning(f"[Player] ffprobe failed for {file_path}: {exc}")
        return default_info


# ── Subtitles (Sidecar .srt & Embedded -> WebVTT) ────────────────────────────

def discover_subtitles(file_path: str, embedded_subs: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """
    Find all sidecar .srt files matching the video stem plus any embedded text subtitles.
    Sidecar files take priority and are listed first.
    """
    subs: list[dict[str, Any]] = []
    parent = os.path.dirname(file_path)
    stem = os.path.splitext(os.path.basename(file_path))[0]

    if os.path.isdir(parent):
        for fname in sorted(os.listdir(parent)):
            if not fname.lower().endswith('.srt') or not fname.startswith(stem):
                continue
            full = os.path.join(parent, fname)
            if not os.path.isfile(full):
                continue
            suffix = fname[len(stem):-4].lstrip('.').lower() or 'en'
            lang_label = LANG_NAMES.get(suffix, suffix.upper())
            subs.append({
                'id': f"srt_{suffix}",
                'lang': suffix,
                'label': f"{lang_label} (.srt)",
                'source': 'sidecar',
                'path': full,
            })

    if embedded_subs:
        subs.extend(embedded_subs)

    return subs


def srt_to_vtt(srt_content: str) -> str:
    """Convert SubRip (.srt) text content into valid HTML5 WebVTT (.vtt)."""
    text = (srt_content or '').lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n')
    # Replace comma decimal separator in SRT timestamps: 00:01:23,456 --> 00:01:25,789
    text = re.sub(
        r'(\d{2}:\d{2}:\d{2}),(\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}),(\d{3})',
        r'\1.\2 --> \3.\4',
        text,
    )
    return "WEBVTT\n\n" + text.strip() + "\n"


def get_subtitle_vtt(
    media_type: str,
    media_id: int,
    sub_id: str,
    season: int | None = None,
    episode: int | None = None,
) -> tuple[str | None, str | None]:
    """Return (vtt_string, error_message) for a sidecar or embedded subtitle track."""
    file_path, _, err = resolve_media_file(media_type, media_id, season, episode)
    if err or not file_path:
        return None, err or 'Media file not found'

    if sub_id.startswith('srt_'):
        subs = discover_subtitles(file_path)
        match = next((s for s in subs if s['id'] == sub_id and s.get('path')), None)
        if not match:
            return None, 'Subtitle file not found'
        raw_bytes = b''
        with open(match['path'], 'rb') as fh:
            raw_bytes = fh.read()
        try:
            srt_text = raw_bytes.decode('utf-8')
        except UnicodeDecodeError:
            srt_text = raw_bytes.decode('latin-1', errors='replace')
        return srt_to_vtt(srt_text), None

    elif sub_id.startswith('emb_') and has_ffmpeg():
        try:
            sub_idx = int(sub_id.split('_', 1)[1])
        except (ValueError, IndexError):
            return None, 'Invalid embedded subtitle index'
        try:
            cmd = [
                'ffmpeg', '-v', 'error',
                '-i', file_path,
                '-map', f'0:s:{sub_idx}',
                '-f', 'webvtt', 'pipe:1',
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=25)
            if proc.returncode == 0 and proc.stdout.strip().startswith('WEBVTT'):
                return proc.stdout, None
            return None, 'Failed extracting embedded subtitle track'
        except Exception as exc:
            return None, f'Subtitle extraction error: {exc}'

    return None, 'Unsupported subtitle track'


# ── Player Info & Next Episode Lookup ────────────────────────────────────────

def get_player_info(
    media_type: str,
    media_id: int,
    season: int | None = None,
    episode: int | None = None,
) -> dict[str, Any]:
    """Build complete player session metadata for the frontend video overlay."""
    file_path, record, err = resolve_media_file(media_type, media_id, season, episode)
    if err or not file_path or not record:
        return {'status': 'error', 'message': err or 'File not available'}

    probe = probe_media_file(file_path)
    subs = discover_subtitles(file_path, probe.get('embedded_subtitles'))
    prog = db.get_playback_progress(media_type, media_id, season, episode)

    duration_sec = probe['duration_sec'] or (float(prog['duration_sec']) if prog else 0.0)
    resume_sec = float(prog['position_sec']) if prog else 0.0
    if duration_sec > 0 and resume_sec >= duration_sec * 0.92:
        resume_sec = 0.0

    next_ep_info = None
    if media_type == 'episode' and season is not None and episode is not None:
        title_str = f"{record['title']} · S{int(season):02d}E{int(episode):02d}"
        sub_title = os.path.basename(file_path)
        # Check if next episode is on disk
        ep_status_data = media_manager.get_show_episodes_status(int(media_id))
        if ep_status_data.get('status') == 'success':
            all_eps = []
            for sec in ep_status_data.get('seasons', []):
                all_eps.extend(sec.get('episodes', []))
            all_eps.sort(key=lambda x: (x['season'], x['episode']))
            for cand in all_eps:
                if (cand['season'], cand['episode']) > (int(season), int(episode)):
                    if cand['status'] == 'downloaded':
                        next_ep_info = {
                            'show_id': int(media_id),
                            'season': cand['season'],
                            'episode': cand['episode'],
                            'title': cand.get('title') or f"S{cand['season']:02d}E{cand['episode']:02d}",
                            'tag': f"S{cand['season']:02d}E{cand['episode']:02d}",
                        }
                    break
    else:
        yr = f" ({record['year']})" if record.get('year') else ''
        title_str = f"{record['title']}{yr}"
        sub_title = os.path.basename(file_path)

    stream_mode = 'direct' if (probe['can_direct_play'] or not has_ffmpeg()) else 'remux'

    return {
        'status': 'success',
        'media_type': media_type,
        'media_id': int(media_id),
        'season': int(season) if season is not None else None,
        'episode': int(episode) if episode is not None else None,
        'title': title_str,
        'filename': sub_title,
        'duration_sec': duration_sec,
        'resume_position_sec': round(resume_sec, 1),
        'stream_mode': stream_mode,
        'video_codec': probe['video_codec'],
        'audio_tracks': probe['audio_tracks'],
        'subtitles': [
            {'id': s['id'], 'lang': s['lang'], 'label': s['label'], 'source': s['source']}
            for s in subs
        ],
        'next_episode': next_ep_info,
    }


# ── HTTP 206 Range Streaming & FFmpeg Direct-Stream Remux ────────────────────

def _stream_file_range(file_path: str, mime_type: str = 'video/mp4') -> Response:
    """Serve a file with HTTP 206 Partial Content byte-range support for native seeking."""
    file_size = os.path.getsize(file_path)
    range_header = request.headers.get('Range', None)

    byte1, byte2 = 0, file_size - 1
    status_code = 200

    if range_header:
        match = re.search(r'bytes=(\d+)-(\d*)', range_header)
        if match:
            byte1 = int(match.group(1))
            if match.group(2):
                byte2 = min(int(match.group(2)), file_size - 1)
            status_code = 206

    if byte1 >= file_size or byte1 > byte2:
        resp = Response(status=416)
        resp.headers['Content-Range'] = f'bytes */{file_size}'
        return resp

    length = byte2 - byte1 + 1

    def generate() -> Generator[bytes, None, None]:
        chunk_size = 256 * 1024
        with open(file_path, 'rb') as fh:
            fh.seek(byte1)
            remaining = length
            while remaining > 0:
                data = fh.read(min(chunk_size, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    resp = Response(generate(), status=status_code, mimetype=mime_type, direct_passthrough=True)
    resp.headers['Accept-Ranges'] = 'bytes'
    resp.headers['Content-Length'] = str(length)
    if status_code == 206:
        resp.headers['Content-Range'] = f'bytes {byte1}-{byte2}/{file_size}'
    return resp


def _stream_ffmpeg_remux(
    file_path: str,
    start_sec: float = 0.0,
    audio_idx: int = 0,
    video_codec: str = 'h264',
    force_transcode: bool = False,
) -> Response:
    """
    On-the-fly FFmpeg direct-stream remuxer / transcoder:
      - When force_transcode=False and codec is H.264/HEVC: copies video bitstream (-c:v copy)
        and writes Apple/LG webOS compliant 'hvc1' tag for HEVC so hardware decoders initialize
      - When force_transcode=True (or unsupported codec): transcodes video to universal 1080p H.264
        (libx264 -preset superfast) for Smart TV browsers whose <video> element rejects HEVC in fMP4
      - Transcodes audio to universal 192kbps stereo AAC (-c:a aac -ac 2)
      - Outputs fragmented MP4 (fMP4) directly to HTTP response pipe
    """
    is_hevc = video_codec in {'hevc', 'h265', 'hev1', 'hvc1'}
    if not force_transcode and video_codec in COPYABLE_VIDEO_CODECS:
        v_codec_args = ['-c:v', 'copy']
        if is_hevc:
            v_codec_args.extend(['-tag:v', 'hvc1'])
    else:
        v_codec_args = [
            '-c:v', 'libx264',
            '-preset', 'superfast',
            '-crf', '22',
            '-pix_fmt', 'yuv420p',
            '-vf', 'scale=min(1920\\,iw):-2',
            '-profile:v', 'high',
            '-level', '4.1',
        ]

    cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error']
    if start_sec > 0.5:
        cmd.extend(['-ss', f"{start_sec:.2f}"])
    cmd.extend([
        '-i', file_path,
        '-map', '0:v:0',
        '-map', f'0:a:{max(0, int(audio_idx))}?',
        *v_codec_args,
        '-c:a', 'aac',
        '-ac', '2',
        '-b:a', '192k',
        '-sn',
        '-avoid_negative_ts', 'make_zero',
        '-reset_timestamps', '1',
        '-movflags', 'frag_keyframe+empty_moov+default_base_moof',
        '-f', 'mp4',
        'pipe:1',
    ])

    def generate() -> Generator[bytes, None, None]:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=256 * 1024,
        )
        try:
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read(128 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    resp = Response(generate(), status=200, mimetype='video/mp4', direct_passthrough=True)
    resp.headers['Cache-Control'] = 'no-store'
    return resp


def stream_media_response(
    media_type: str,
    media_id: int,
    season: int | None = None,
    episode: int | None = None,
    start_sec: float = 0.0,
    audio_idx: int = 0,
    force_transcode: bool = False,
) -> Response:
    """Entry point for GET /api/player/stream/<media_type>/<media_id>."""
    file_path, _, err = resolve_media_file(media_type, media_id, season, episode)
    if err or not file_path:
        return Response(err or 'Media file not found', status=404)

    probe = probe_media_file(file_path)

    # Fast Path 1: Native MP4 + AAC with default audio track and no FFmpeg seek offset
    if (not force_transcode and probe['can_direct_play'] and start_sec <= 0.5 and audio_idx == 0) or not has_ffmpeg():
        return _stream_file_range(file_path, mime_type='video/mp4')

    # Fast Path 2: Zero-CPU video copy (-tag:v hvc1 for HEVC) or H.264 compatibility transcode + AAC audio
    return _stream_ffmpeg_remux(
        file_path=file_path,
        start_sec=start_sec,
        audio_idx=audio_idx,
        video_codec=probe['video_codec'],
        force_transcode=force_transcode,
    )


# ── Active Stream Session Registry & Watched Lifecycle ───────────────────────

def _session_key(media_type: str, media_id: int, season: int | None = None, episode: int | None = None) -> str:
    return f"{media_type}:{int(media_id)}:{int(season or 0)}:{int(episode or 0)}"


def get_active_streams() -> list[dict[str, Any]]:
    """Return active built-in player streams formatted for Dashboard & Shutdown Watcher."""
    now = time.time()
    out: list[dict[str, Any]] = []
    with _sessions_lock:
        expired = [k for k, v in _active_sessions.items() if now - v['updated_ts'] > SESSION_TTL_SEC]
        for k in expired:
            _active_sessions.pop(k, None)

        for sess in _active_sessions.values():
            out.append({
                'user': 'Rolarr',
                'user_thumb': '',
                'device': sess.get('device') or '[Rolarr Player] Web Browser',
                'state': sess.get('state', 'playing'),
                'title': sess.get('title', 'Media'),
                'progress_pct': sess.get('progress_pct', 0.0),
                'remaining_mins': sess.get('remaining_mins', 0),
                'decision': sess.get('decision', 'DIRECT STREAM'),
                'location': 'LAN',
                'bandwidth': 'Built-in',
            })
    return out


def record_heartbeat(data: dict[str, Any]) -> dict[str, Any]:
    """
    Record a playback heartbeat from the built-in player:
      - Updates SQLite playback_progress for resume support
      - Registers/refreshes active stream session (pauses Host Shutdown Watcher)
      - If state == 'stopped', removes active session
      - If progress >= 90% and state == 'ended' (or auto_watched=True), recycles watched media
    """
    media_type = (data.get('media_type') or '').lower()
    media_id = int(data.get('media_id') or 0)
    season = int(data['season']) if data.get('season') is not None else None
    episode = int(data['episode']) if data.get('episode') is not None else None
    position_sec = max(0.0, float(data.get('position_sec') or 0.0))
    duration_sec = max(0.0, float(data.get('duration_sec') or 0.0))
    state = (data.get('state') or 'playing').lower()
    title = data.get('title') or f"{media_type.title()} #{media_id}"
    stream_mode = (data.get('stream_mode') or 'remux').lower()

    if not media_type or not media_id:
        return {'status': 'error', 'message': 'Missing media_type or media_id'}

    key = _session_key(media_type, media_id, season, episode)

    if state == 'stopped':
        with _sessions_lock:
            _active_sessions.pop(key, None)
        if position_sec > 5.0 and (duration_sec <= 0 or position_sec < duration_sec * 0.90):
            db.save_playback_progress(media_type, media_id, position_sec, duration_sec, season, episode)
        return {'status': 'success', 'watched_triggered': False}

    pct = round((position_sec / duration_sec * 100.0), 1) if duration_sec > 0 else 0.0
    rem_mins = round(max(0.0, duration_sec - position_sec) / 60.0) if duration_sec > 0 else 0

    with _sessions_lock:
        _active_sessions[key] = {
            'updated_ts': time.time(),
            'title': title,
            'state': 'paused' if state == 'paused' else 'playing',
            'progress_pct': min(100.0, pct),
            'remaining_mins': rem_mins,
            'decision': 'DIRECT PLAY' if stream_mode == 'direct' else 'DIRECT STREAM',
            'device': '[Rolarr Player] Web / TV Browser',
        }

    # Auto-trigger watched lifecycle when playback finishes or crosses 92% on 'ended'
    if state == 'ended' or (duration_sec > 60 and pct >= 92.0 and data.get('allow_auto_watch', False)):
        with _sessions_lock:
            _active_sessions.pop(key, None)
        res = mark_media_watched(media_type, media_id, season, episode)
        return {'status': 'success', 'watched_triggered': True, 'watched_result': res}

    if position_sec > 5.0:
        db.save_playback_progress(media_type, media_id, position_sec, duration_sec, season, episode)

    return {'status': 'success', 'watched_triggered': False, 'progress_pct': pct}


def mark_media_watched(
    media_type: str,
    media_id: int,
    season: int | None = None,
    episode: int | None = None,
) -> dict[str, Any]:
    """
    Mark an episode or movie as watched from Rolarr's UI / built-in player:
      1. Clear saved playback progress and active stream entry
      2. Delete watched file from disk if DELETE_WATCHED_EPISODES is enabled
      3. Refresh Plex / Jellyfin libraries if configured
      4. Advance show watch cursor & queue next ROLLING_WINDOW aired episodes
    """
    media_type = (media_type or '').lower()
    key = _session_key(media_type, int(media_id), season, episode)
    with _sessions_lock:
        _active_sessions.pop(key, None)

    db.clear_playback_progress(media_type, int(media_id), season, episode)
    delete_enabled = bool(get_config('DELETE_WATCHED_EPISODES', True))

    if media_type == 'episode':
        show = db.get_show(int(media_id))
        if not show or season is None or episode is None:
            return {'status': 'error', 'message': 'Show or episode not found'}
        s_num, e_num = int(season), int(episode)
        ep_str = f"S{s_num:02d}E{e_num:02d}"

        if delete_enabled:
            deleted, detail = media_manager.delete_episode_file(show['title'], s_num, e_num)
            file_action = detail if deleted else f"cleanup skipped ({detail})"
        else:
            file_action = "delete disabled (file kept on disk)"

        db.update_show_watch_cursor(show['id'], s_num, e_num)

        episodes = metadata.get_show_episodes(show['tvmaze_id'])
        aired = [e for e in episodes if e['aired']]
        upcoming = [e for e in aired if (e['season'], e['episode']) > (s_num, e_num)]
        window = upcoming[:media_manager.get_rolling_window()]

        queued_eps = []
        for nxt in window:
            tag = f"S{nxt['season']:02d}E{nxt['episode']:02d}"
            ok, _ = media_manager.queue_episode(show, nxt['season'], nxt['episode'])
            if ok:
                queued_eps.append(tag)

        _increment_processed_counter()
        _notify_external_media_servers()

        q_note = f"; queued next: {', '.join(queued_eps)}" if queued_eps else ""
        msg = f"{show['title']} {ep_str} watched — {file_action}{q_note}"
        _activity_log('success', msg)
        return {'status': 'success', 'message': msg, 'queued': queued_eps}

    elif media_type == 'movie':
        movie = db.get_movie(int(media_id))
        if not movie:
            return {'status': 'error', 'message': 'Movie not found'}
        title = movie['title']
        year = movie.get('year')
        yr_str = f" ({year})" if year else ""

        if delete_enabled:
            deleted, detail = media_manager.delete_movie_file(title, year, reported_path=movie.get('file_path'))
            file_action = detail if deleted else f"cleanup skipped ({detail})"
            db.mark_movie_watched_by_title(title, year)
        else:
            file_action = "delete disabled (file kept on disk)"

        _increment_processed_counter()
        _notify_external_media_servers()

        msg = f"Movie: {title}{yr_str} watched — {file_action}"
        _activity_log('success', msg)
        return {'status': 'success', 'message': msg}

    return {'status': 'error', 'message': 'Invalid media type'}


def _increment_processed_counter():
    try:
        total = int(get_config('STATS_MEDIA_PROCESSED', 0) or 0) + 1
        save_config({'STATS_MEDIA_PROCESSED': total})
    except Exception:
        pass


def _notify_external_media_servers():
    try:
        from services.plex_poller import trigger_plex_refresh, _jellyfin_refresh_library
        trigger_plex_refresh()
        _jellyfin_refresh_library()
    except Exception:
        pass
