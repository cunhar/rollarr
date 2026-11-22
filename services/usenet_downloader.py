"""
services/usenet_downloader.py
-----------------------------
Native Python Usenet (NNTP) downloader & post-processor replacing NZBGet.

Features:
  - Parses standard .nzb XML files
  - Multi-connection TLS/SSL NNTP client pool (stdlib socket + ssl)
  - Fast bytes.translate yEnc decoder with CRC32 verification
  - PAR2 verification/repair via `par2` CLI (`par2cmdline`)
  - RAR/7z/ZIP extraction via `unrar` / `7z` CLI (fails fast on passworded archives)
  - Automatic video selection, clean renaming & moving to PATH_TV / PATH_MOVIES
  - Plex & Jellyfin library refresh on completion
  - Failed-release blocklisting + automatic retry of next-best NZB
"""
from __future__ import annotations

import os
import re
import ssl
import time
import zlib
import queue
import shutil
import socket
import logging
import threading
import subprocess
import concurrent.futures
import xml.etree.ElementTree as ET
from typing import Any

from config_store import get_config
from integrations.common import sanitize_filename
from integrations.nzbgeek import download_nzb_bytes
from services import db

logger = logging.getLogger(__name__)

VIDEO_EXTS = {'.mkv', '.mp4', '.avi', '.m4v', '.ts', '.mov', '.wmv'}
MAX_RETRIES = 3

# ── Fast yEnc Decoder ────────────────────────────────────────────────────────

_YENC_TABLE = bytes((b - 42) & 0xFF for b in range(256))
_YENC_ESC_TABLE = bytes((b - 64 - 42) & 0xFF for b in range(256))
_RE_YPARAM = re.compile(r'(\w+)=([^\s]+)')


def decode_yenc(raw_bytes: bytes) -> dict[str, Any]:
    """
    Decode a yEnc-encoded NNTP article body.
    Returns dict with: data (bytes), name (str), part (int|None), begin (int),
    crc_ok (bool), crc32 (int).
    """
    lines = raw_bytes.splitlines()
    in_body = False
    header: dict[str, str] = {}
    part_hdr: dict[str, str] = {}
    trailer: dict[str, str] = {}
    body_chunks: list[bytes] = []

    for line in lines:
        if line.startswith(b'=ybegin '):
            in_body = True
            text = line[8:].decode('latin-1', errors='replace')
            # name= is always the last field and may contain spaces
            name_idx = text.find('name=')
            if name_idx != -1:
                header['name'] = text[name_idx + 5:].strip()
                text = text[:name_idx]
            for k, v in _RE_YPARAM.findall(text):
                header[k] = v
            continue
        if line.startswith(b'=ypart '):
            text = line[7:].decode('latin-1', errors='replace')
            for k, v in _RE_YPARAM.findall(text):
                part_hdr[k] = v
            continue
        if line.startswith(b'=yend '):
            text = line[6:].decode('latin-1', errors='replace')
            for k, v in _RE_YPARAM.findall(text):
                trailer[k] = v
            break
        if in_body:
            body_chunks.append(line)

    encoded = b''.join(body_chunks)
    if b'=' not in encoded:
        decoded = encoded.translate(_YENC_TABLE)
    else:
        # Handle escape sequences (=X -> (X - 64 - 42) & 0xFF)
        out = bytearray()
        parts = encoded.split(b'=')
        out.extend(parts[0].translate(_YENC_TABLE))
        for seg in parts[1:]:
            if not seg:
                continue
            out.append(_YENC_ESC_TABLE[seg[0]])
            if len(seg) > 1:
                out.extend(seg[1:].translate(_YENC_TABLE))
        decoded = bytes(out)

    calc_crc = zlib.crc32(decoded) & 0xFFFFFFFF
    expected_crc_hex = trailer.get('pcrc32') or trailer.get('crc32')
    crc_ok = True
    if expected_crc_hex:
        try:
            crc_ok = (calc_crc == int(expected_crc_hex, 16))
        except ValueError:
            crc_ok = True

    begin_offset = max(0, int(part_hdr.get('begin', '1')) - 1) if part_hdr else 0
    return {
        'data': decoded,
        'name': header.get('name', ''),
        'part': int(header['part']) if 'part' in header and header['part'].isdigit() else None,
        'begin': begin_offset,
        'crc_ok': crc_ok,
        'crc32': calc_crc,
    }


# ── NZB XML Parser ───────────────────────────────────────────────────────────

_RE_SUBJECT_FILENAME = re.compile(r'"([^"]+)"')


def parse_nzb(nzb_bytes: bytes) -> list[dict[str, Any]]:
    """
    Parse an .nzb XML payload into a list of file dicts:
      [{filename, subject, segments: [{number, bytes, msgid}], total_bytes}]
    """
    root = ET.fromstring(nzb_bytes)
    files: list[dict[str, Any]] = []

    for elem in root.iter():
        if not elem.tag.endswith('file'):
            continue
        subject = elem.attrib.get('subject', '')
        m = _RE_SUBJECT_FILENAME.search(subject)
        filename = m.group(1) if m else f"file_{len(files) + 1}.bin"
        segments = []
        for child in elem.iter():
            if child.tag.endswith('segment') and child.text:
                segments.append({
                    'number': int(child.attrib.get('number', len(segments) + 1)),
                    'bytes': int(child.attrib.get('bytes', 0)),
                    'msgid': child.text.strip(),
                })
        segments.sort(key=lambda s: s['number'])
        if segments:
            files.append({
                'filename': filename,
                'subject': subject,
                'segments': segments,
                'total_bytes': sum(s['bytes'] for s in segments),
            })
    return files


# ── Raw NNTP Client ──────────────────────────────────────────────────────────

class NNTPConnection:
    """Minimal, fast, thread-local NNTP client using stdlib socket + ssl."""

    def __init__(self, host: str, port: int, use_ssl: bool, username: str, password: str, timeout: int = 20):
        self.host = host
        self.port = int(port)
        self.use_ssl = bool(use_ssl)
        self.username = username
        self.password = password
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.rfile = None

    def connect(self):
        self.close()
        raw_sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        if self.use_ssl:
            ctx = ssl.create_default_context()
            self.sock = ctx.wrap_socket(raw_sock, server_hostname=self.host)
        else:
            self.sock = raw_sock
        self.rfile = self.sock.makefile('rb', buffering=65536)
        greeting = self._readline()
        if not greeting.startswith((b'200', b'201')):
            raise ConnectionError(f"Unexpected NNTP greeting: {greeting.decode(errors='replace')}")
        if self.username:
            self._send(f"AUTHINFO USER {self.username}\r\n")
            resp = self._readline()
            if resp.startswith(b'381'):
                self._send(f"AUTHINFO PASS {self.password}\r\n")
                resp = self._readline()
            if not resp.startswith(b'281'):
                raise ConnectionError(f"NNTP auth failed: {resp.decode(errors='replace')}")

    def _send(self, cmd: str):
        assert self.sock is not None
        self.sock.sendall(cmd.encode('utf-8'))

    def _readline(self) -> bytes:
        assert self.rfile is not None
        line = self.rfile.readline()
        if not line:
            raise ConnectionError("NNTP connection closed unexpectedly")
        return line.rstrip(b'\r\n')

    def fetch_body(self, msgid: str) -> bytes | None:
        """Fetch article body by Message-ID. Returns None if article missing (430)."""
        if self.sock is None:
            self.connect()
        clean_id = msgid.strip()
        if not clean_id.startswith('<'):
            clean_id = f"<{clean_id}>"
        try:
            self._send(f"BODY {clean_id}\r\n")
            resp = self._readline()
        except Exception:
            self.connect()
            self._send(f"BODY {clean_id}\r\n")
            resp = self._readline()

        if resp.startswith(b'430') or resp.startswith(b'423'):
            return None
        if not resp.startswith(b'222'):
            raise RuntimeError(f"NNTP BODY failed: {resp.decode(errors='replace')}")

        chunks: list[bytes] = []
        while True:
            line = self.rfile.readline()
            if not line:
                raise ConnectionError("NNTP stream truncated mid-article")
            if line in (b'.\r\n', b'.\n'):
                break
            if line.startswith(b'..'):
                line = line[1:]
            chunks.append(line)
        return b''.join(chunks)

    def close(self):
        try:
            if self.sock:
                try:
                    self._send("QUIT\r\n")
                except Exception:
                    pass
                self.sock.close()
        finally:
            self.sock = None
            self.rfile = None


def test_nntp_connection(host: str, port: int, use_ssl: bool, username: str, password: str) -> tuple[bool, str]:
    """Verify NNTP server connectivity and credentials."""
    if not host:
        return False, 'Usenet NNTP Host is required'
    conn = NNTPConnection(host, port, use_ssl, username, password, timeout=8)
    try:
        conn.connect()
        return True, f"Connected to Usenet server {host}:{port} ({'SSL' if use_ssl else 'Plain'})"
    except Exception as exc:
        return False, f"Usenet NNTP error: {exc}"
    finally:
        conn.close()


# ── Downloader Service State ─────────────────────────────────────────────────

_state_lock = threading.Lock()
_wake_event = threading.Event()
_paused = False
_cancelled_ids: set[int] = set()
_log_callback = None

_live_state: dict[str, Any] = {
    'active_job_id': None,
    'download_rate_bps': 0,
    'current_stage': '',
    'disk_guard_paused': False,
    'disk_guard_reason': '',
}


def set_log_callback(fn):
    global _log_callback
    _log_callback = fn


def _activity_log(status: str, message: str, payload: dict | None = None):
    logger.info(f"[UsenetDL] {message}")
    if _log_callback:
        try:
            _log_callback(status, message, payload)
        except Exception:
            pass


def notify_queue():
    """Wake the downloader worker thread immediately."""
    _wake_event.set()


def pause():
    global _paused
    with _state_lock:
        _paused = True
    _activity_log('info', 'Usenet downloader paused')


def resume():
    global _paused
    with _state_lock:
        _paused = False
    _wake_event.set()
    _activity_log('info', 'Usenet downloader resumed')


def cancel_download(download_id: int) -> bool:
    job = db.get_download(download_id)
    if not job:
        return False
    with _state_lock:
        _cancelled_ids.add(download_id)
    if job['status'] == 'queued':
        db.update_download(download_id, status='cancelled', error='Cancelled by user')
    _activity_log('info', f"Cancelled download #{download_id}: {job['nzb_title']}")
    return True


def is_nntp_configured() -> bool:
    return bool((get_config('NNTP_HOST') or '').strip())


def _format_rate(bps: int) -> str:
    if bps >= 1024 * 1024:
        return f"{bps / (1024 * 1024):.1f} MB/s"
    if bps >= 1024:
        return f"{bps / 1024:.0f} KB/s"
    return f"{bps} B/s"


def get_status() -> dict[str, Any]:
    """Return structured downloader status for Dashboard UI and Power Saver watcher."""
    configured = is_nntp_configured()
    active_jobs = db.list_active_downloads()

    with _state_lock:
        rate_bps = _live_state['download_rate_bps'] if not _paused and active_jobs else 0
        is_paused = _paused
        dg_paused = _live_state['disk_guard_paused']
        dg_reason = _live_state['disk_guard_reason']

    downloads = []
    for job in active_jobs:
        size_mb = round((job.get('size_bytes') or 0) / (1024 * 1024), 1)
        done_mb = round((job.get('downloaded_bytes') or 0) / (1024 * 1024), 1)
        rem_mb = max(0.0, round(size_mb - done_mb, 1))
        pct = round(float(job.get('progress_pct') or 0.0), 1)

        if rate_bps > 0 and rem_mb > 0 and job['status'] == 'downloading':
            eta_secs = int((rem_mb * 1024 * 1024) / rate_bps)
            eta_mins = round(eta_secs / 60)
            eta_str = f"{eta_mins} min left" if eta_mins > 0 else "< 1 min left"
        else:
            eta_str = "—"

        status_label = 'PAUSED' if is_paused else job['status'].upper()
        cat = 'TV' if job['media_type'] == 'episode' else 'Movie'
        downloads.append({
            'id': job['id'],
            'name': job['nzb_title'],
            'media_label': (
                f"{job['title']} S{job['season']:02d}E{job['episode']:02d}"
                if job['media_type'] == 'episode' and job.get('season') is not None
                else f"{job['title']} ({job.get('year') or ''})".strip()
            ),
            'size_mb': size_mb,
            'remaining_mb': rem_mb,
            'progress_pct': pct,
            'status': status_label,
            'eta': eta_str,
            'category': cat,
        })

    return {
        'enabled': configured,
        'connected': configured,
        'status_text': 'Ready' if configured else 'Not Configured',
        'download_rate': _format_rate(rate_bps),
        'download_rate_bps': rate_bps,
        'download_paused': is_paused,
        'disk_guard_paused': dg_paused,
        'disk_guard_reason': dg_reason,
        'downloads': downloads,
    }


# ── Post-Processing Helpers ──────────────────────────────────────────────────

def _run_par2(work_dir: str) -> tuple[bool, str]:
    """Run par2 repair if .par2 files are present and `par2` binary is available."""
    par2_files = [f for f in os.listdir(work_dir) if f.lower().endswith('.par2')]
    if not par2_files:
        return True, 'No PAR2 files'

    par2_bin = shutil.which('par2')
    if not par2_bin:
        logger.warning("[UsenetDL] `par2` binary not installed; skipping PAR2 verification")
        return True, 'par2 not installed'

    # Prefer the shortest base .par2 file (not .vol00+01.par2)
    base_par2 = sorted(par2_files, key=lambda f: ('.vol' in f.lower(), len(f)))[0]
    cmd = [par2_bin, 'r', '-q', os.path.join(work_dir, base_par2)]
    res = subprocess.run(cmd, cwd=work_dir, capture_output=True, text=True, timeout=600)
    if res.returncode == 0:
        return True, 'PAR2 verified/repaired'
    return False, (res.stderr or res.stdout or 'PAR2 repair failed')[:300]


def _unpack_archives(work_dir: str, extract_dir: str) -> tuple[bool, str]:
    """Extract RAR / 7z / ZIP archives found in work_dir into extract_dir."""
    os.makedirs(extract_dir, exist_ok=True)
    entries = sorted(os.listdir(work_dir))

    rar_targets = []
    other_archives = []
    for name in entries:
        low = name.lower()
        if low.endswith('.rar'):
            # For split .partXX.rar, only extract part01 / part001
            m = re.search(r'\.part(\d+)\.rar$', low)
            if m and int(m.group(1)) != 1:
                continue
            rar_targets.append(os.path.join(work_dir, name))
        elif low.endswith(('.7z', '.zip', '.7z.001')):
            other_archives.append(os.path.join(work_dir, name))

    unrar_bin = shutil.which('unrar')
    sevenz_bin = shutil.which('7z') or shutil.which('7zz') or shutil.which('7za')

    for rar_path in rar_targets:
        if unrar_bin:
            res = subprocess.run(
                [unrar_bin, 'x', '-o+', '-p-', '-y', rar_path, extract_dir + os.sep],
                capture_output=True, text=True, timeout=600,
            )
        elif sevenz_bin:
            res = subprocess.run(
                [sevenz_bin, 'x', '-y', '-p', f'-o{extract_dir}', rar_path],
                capture_output=True, text=True, timeout=600,
            )
        else:
            return False, 'Neither unrar nor 7z is installed'

        if res.returncode != 0:
            err = (res.stderr or res.stdout or '').lower()
            if 'password' in err or 'encrypted' in err:
                return False, 'Password-protected archive'
            return False, f"Archive extraction failed: {(res.stderr or res.stdout)[:200]}"

    for arc_path in other_archives:
        if not sevenz_bin:
            return False, '7z binary not installed'
        res = subprocess.run(
            [sevenz_bin, 'x', '-y', '-p', f'-o{extract_dir}', arc_path],
            capture_output=True, text=True, timeout=600,
        )
        if res.returncode != 0:
            return False, f"Archive extraction failed: {(res.stderr or res.stdout)[:200]}"

    return True, 'Unpacked'


def _find_largest_video(search_dirs: list[str]) -> str | None:
    """Locate the largest non-sample video file across downloaded/extracted folders."""
    best_path = None
    best_size = 0
    for root_dir in search_dirs:
        if not os.path.isdir(root_dir):
            continue
        for dirpath, _, filenames in os.walk(root_dir):
            for fname in filenames:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in VIDEO_EXTS:
                    continue
                if 'sample' in fname.lower():
                    continue
                full = os.path.join(dirpath, fname)
                try:
                    sz = os.path.getsize(full)
                except OSError:
                    continue
                if sz > best_size:
                    best_size = sz
                    best_path = full
    return best_path


def _move_to_library(job: dict[str, Any], video_path: str) -> str:
    """Rename cleanly and move the completed video into PATH_TV or PATH_MOVIES."""
    ext = os.path.splitext(video_path)[1].lower() or '.mkv'
    safe_title = sanitize_filename(job['title'])

    if job['media_type'] == 'episode':
        base_tv = get_config('PATH_TV', '/tv') or '/tv'
        season = int(job['season'])
        episode = int(job['episode'])
        dest_dir = os.path.join(base_tv, safe_title, f"Season {season:02d}")
        dest_name = f"{safe_title} - S{season:02d}E{episode:02d}{ext}"
    else:
        base_movies = get_config('PATH_MOVIES', '/movies') or '/movies'
        year = job.get('year')
        folder_name = f"{safe_title} ({year})" if year else safe_title
        dest_dir = os.path.join(base_movies, folder_name)
        dest_name = f"{folder_name}{ext}"

    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, dest_name)
    shutil.move(video_path, dest_path)
    return dest_path


def _notify_media_servers():
    try:
        from services.plex_poller import trigger_plex_refresh, _jellyfin_refresh_library
        trigger_plex_refresh()
        _jellyfin_refresh_library()
    except Exception as exc:
        logger.debug(f"[UsenetDL] Media server refresh error: {exc}")


# ── Core Job Execution ───────────────────────────────────────────────────────

def _process_job(job: dict[str, Any]):
    job_id = job['id']
    host = (get_config('NNTP_HOST') or '').strip()
    port = int(get_config('NNTP_PORT', 563) or 563)
    use_ssl = bool(get_config('NNTP_SSL', True))
    username = (get_config('NNTP_USERNAME') or '').strip()
    password = get_config('NNTP_PASSWORD') or ''
    max_conns = max(1, min(30, int(get_config('NNTP_CONNECTIONS', 8) or 8)))

    if not host:
        db.update_download(job_id, status='failed', error='NNTP server not configured')
        return

    dl_root = get_config('PATH_DOWNLOADS', '/downloads') or '/downloads'
    work_dir = os.path.join(dl_root, 'tmp', f"job_{job_id}")
    extract_dir = os.path.join(work_dir, '_extracted')
    os.makedirs(work_dir, exist_ok=True)

    try:
        db.update_download(job_id, status='downloading', progress_pct=0.0, downloaded_bytes=0)
        _activity_log('info', f"Starting Usenet download: {job['nzb_title']}")

        nzb_bytes = download_nzb_bytes(job['nzb_url'])
        files = parse_nzb(nzb_bytes)
        if not files:
            raise RuntimeError("NZB contained no downloadable files")

        # Put non-par2 files first, then .par2 index, then recovery volumes
        files.sort(key=lambda f: (
            2 if '.vol' in f['filename'].lower() and f['filename'].lower().endswith('.par2')
            else (1 if f['filename'].lower().endswith('.par2') else 0)
        ))

        total_bytes = sum(f['total_bytes'] for f in files)
        if total_bytes > 0:
            db.update_download(job_id, size_bytes=total_bytes)

        # Prepare tasks: list of (file_idx, file_meta, segment)
        file_locks: dict[int, threading.Lock] = {i: threading.Lock() for i in range(len(files))}
        file_paths: dict[int, str] = {}
        tasks: queue.Queue = queue.Queue()
        for f_idx, fmeta in enumerate(files):
            for seg in fmeta['segments']:
                tasks.put((f_idx, fmeta, seg))

        downloaded_bytes = 0
        missing_segments = 0
        crc_errors = 0
        total_segments = tasks.qsize()
        progress_lock = threading.Lock()
        rate_window_start = time.time()
        rate_window_bytes = 0
        last_db_update = 0.0

        def worker():
            nonlocal downloaded_bytes, missing_segments, crc_errors, rate_window_start, rate_window_bytes, last_db_update
            conn = NNTPConnection(host, port, use_ssl, username, password)
            try:
                conn.connect()
                while True:
                    with _state_lock:
                        if job_id in _cancelled_ids:
                            return
                        is_p = _paused
                    if is_p:
                        time.sleep(0.5)
                        continue

                    try:
                        f_idx, fmeta, seg = tasks.get_nowait()
                    except queue.Empty:
                        return

                    try:
                        raw = conn.fetch_body(seg['msgid'])
                        if raw is None:
                            with progress_lock:
                                missing_segments += 1
                            continue

                        dec = decode_yenc(raw)
                        if not dec['crc_ok']:
                            with progress_lock:
                                crc_errors += 1

                        target_name = sanitize_filename(dec['name'] or fmeta['filename'])
                        with file_locks[f_idx]:
                            fpath = file_paths.get(f_idx)
                            if not fpath:
                                fpath = os.path.join(work_dir, target_name)
                                file_paths[f_idx] = fpath
                            mode = 'r+b' if os.path.exists(fpath) else 'wb'
                            with open(fpath, mode) as fh:
                                fh.seek(dec['begin'])
                                fh.write(dec['data'])

                        seg_len = len(dec['data'])
                        now_t = time.time()
                        with progress_lock:
                            downloaded_bytes += seg_len
                            rate_window_bytes += seg_len
                            elapsed = now_t - rate_window_start
                            if elapsed >= 1.0:
                                bps = int(rate_window_bytes / elapsed)
                                with _state_lock:
                                    _live_state['download_rate_bps'] = bps
                                rate_window_start = now_t
                                rate_window_bytes = 0
                            if now_t - last_db_update >= 2.0:
                                pct = min(99.0, (downloaded_bytes / total_bytes * 100.0)) if total_bytes > 0 else 0.0
                                db.update_download(job_id, downloaded_bytes=downloaded_bytes, progress_pct=round(pct, 1))
                                last_db_update = now_t
                    except Exception as exc:
                        logger.debug(f"[UsenetDL] Segment fetch error: {exc}")
                        with progress_lock:
                            missing_segments += 1
                    finally:
                        tasks.task_done()
            finally:
                conn.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_conns) as pool:
            futs = [pool.submit(worker) for _ in range(max_conns)]
            for f in futs:
                f.result()

        with _state_lock:
            _live_state['download_rate_bps'] = 0
            if job_id in _cancelled_ids:
                _cancelled_ids.discard(job_id)
                db.update_download(job_id, status='cancelled', error='Cancelled by user')
                return

        if total_segments > 0 and missing_segments / total_segments > 0.25:
            raise RuntimeError(f"Too many missing articles ({missing_segments}/{total_segments})")

        # PAR2 verify/repair if any segments were missing or had CRC errors
        if missing_segments > 0 or crc_errors > 0:
            db.update_download(job_id, status='verifying', progress_pct=99.0)
            ok, msg = _run_par2(work_dir)
            if not ok:
                raise RuntimeError(f"PAR2 repair failed: {msg}")

        # Unpack archives
        db.update_download(job_id, status='unpacking', progress_pct=99.5)
        ok, msg = _unpack_archives(work_dir, extract_dir)
        if not ok:
            raise RuntimeError(msg)

        # Find video file & move to library
        video_path = _find_largest_video([extract_dir, work_dir])
        if not video_path:
            raise RuntimeError("No video file found after extraction")

        dest_path = _move_to_library(job, video_path)
        db.update_download(job_id, status='completed', progress_pct=100.0, downloaded_bytes=total_bytes or downloaded_bytes)
        if job['media_type'] == 'movie' and job.get('media_id'):
            db.update_movie_status(job['media_id'], 'downloaded', file_path=dest_path)

        # Auto-download subtitles (best-effort, never fails the import)
        sub_note = ''
        try:
            from services import subtitle_service
            imdb_id = None
            if job['media_type'] == 'episode' and job.get('media_id'):
                show_rec = db.get_show(int(job['media_id']))
                imdb_id = show_rec.get('imdb_id') if show_rec else None
            elif job['media_type'] == 'movie' and job.get('media_id'):
                mov_rec = db.get_movie(int(job['media_id']))
                imdb_id = mov_rec.get('imdb_id') if mov_rec else None
            got_subs = subtitle_service.download_subtitles_for_media(
                video_path=dest_path,
                media_type=job['media_type'],
                imdb_id=imdb_id,
                season=job.get('season'),
                episode=job.get('episode'),
            )
            if got_subs:
                sub_note = f" + subs ({', '.join(got_subs)})"
        except Exception as sub_exc:
            logger.debug(f"[UsenetDL] Subtitle auto-download error: {sub_exc}")

        _activity_log('success', f"Completed & imported: {os.path.basename(dest_path)}{sub_note}", {'path': dest_path})
        _notify_media_servers()

    except Exception as exc:
        err_msg = str(exc)
        logger.warning(f"[UsenetDL] Job #{job_id} failed: {err_msg}")
        db.update_download(job_id, status='failed', error=err_msg)
        db.add_to_blocklist(job['nzb_guid'], job['nzb_title'], err_msg)
        _activity_log('warning', f"Download failed ({err_msg}) — blocklisted '{job['nzb_title']}'")

        # Automatic retry with next-best release
        retry_count = int(job.get('retry_count') or 0)
        if retry_count < MAX_RETRIES:
            try:
                from services.media_manager import retry_failed_download
                retry_failed_download(job)
            except Exception as r_exc:
                logger.warning(f"[UsenetDL] Retry scheduling failed: {r_exc}")
    finally:
        with _state_lock:
            _live_state['download_rate_bps'] = 0
            _live_state['active_job_id'] = None
        shutil.rmtree(work_dir, ignore_errors=True)


def _worker_loop():
    logger.info("[UsenetDL] Background download worker started.")
    db.reset_interrupted_downloads()

    while True:
        try:
            with _state_lock:
                is_p = _paused
            if is_p or not is_nntp_configured():
                _wake_event.wait(timeout=5)
                _wake_event.clear()
                continue

            from services.disk_service import check_disk_space_guard
            low_disk, low_reason = check_disk_space_guard()
            with _state_lock:
                prev_dg = _live_state['disk_guard_paused']
                _live_state['disk_guard_paused'] = low_disk
                _live_state['disk_guard_reason'] = low_reason
            if low_disk:
                if not prev_dg:
                    _activity_log('warning', f"Disk Space Guard paused downloads: {low_reason}")
                _wake_event.wait(timeout=30)
                _wake_event.clear()
                continue

            job = db.get_next_queued_download()
            if job:
                with _state_lock:
                    _live_state['active_job_id'] = job['id']
                _process_job(job)
            else:
                _wake_event.wait(timeout=5)
                _wake_event.clear()
        except Exception as exc:
            logger.error(f"[UsenetDL] Unhandled worker exception: {exc}")
            time.sleep(5)


def start():
    t = threading.Thread(target=_worker_loop, name='usenet-downloader', daemon=True)
    t.start()
