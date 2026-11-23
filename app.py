from flask import Flask, request, jsonify, render_template, Response

import os
import logging
from logging.handlers import RotatingFileHandler
import concurrent.futures

import config_store
from integrations import metadata
from services import db, plex_watcher, plex_poller, activity_log, usenet_downloader, media_manager, player_service
from services.disk_service import get_disk_space_summary
from services.connection_tester import (
    test_connection,
    check_nzbgeek_status,
    check_usenet_status,
    check_plex_status,
    check_jellyfin_status,
)

# Persistent configuration directories
CONFIG_DIR = '/config'
if not os.path.exists(CONFIG_DIR):
    CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))

LOG_FILE = os.path.join(CONFIG_DIR, 'rolarr.log')

# Configure logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

try:
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=5)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
except Exception as e:
    print(f"Could not initialize file logging: {e}")

app = Flask(__name__)

# Initialize SQLite database, activity log, and background services
db.init_db()
activity_log.load()
plex_poller.set_log_callback(activity_log.log_call)
plex_watcher.set_log_callback(activity_log.log_call)
usenet_downloader.set_log_callback(activity_log.log_call)
media_manager.set_autograb_log_callback(activity_log.log_call)
player_service.set_log_callback(activity_log.log_call)

if not os.environ.get('ROLARR_NO_BACKGROUND'):
    plex_poller.start()
    plex_watcher.start()
    usenet_downloader.start()
    media_manager.start_autograb()


# ── Live Service Connection Checks ───────────────────────────────────────────

def get_service_connections() -> dict:
    """Run connection checks concurrently to reduce dashboard latency."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        f_nzbgeek  = executor.submit(check_nzbgeek_status)
        f_usenet   = executor.submit(check_usenet_status)
        f_plex     = executor.submit(check_plex_status)
        f_jellyfin = executor.submit(check_jellyfin_status)
        return {
            'nzbgeek':  f_nzbgeek.result(),
            'usenet':   f_usenet.result(),
            'plex':     f_plex.result(),
            'jellyfin': f_jellyfin.result(),
        }


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    cfg = config_store.get_all_config()
    conns = get_service_connections()

    return render_template(
        "index.html",
        cfg=cfg,
        nzbgeek_conn=conns['nzbgeek'],
        usenet_conn=conns['usenet'],
        plex_conn_text=conns['plex']['status'],
        jellyfin_conn_text=conns['jellyfin']['status'],
        downloads_status=usenet_downloader.get_status(),
        history=activity_log.get_entries(),
        rolling_window=media_manager.get_rolling_window(),
        plex_status=plex_watcher.get_state(),
        poller_status=plex_poller.get_state(),
        disk_info=get_disk_space_summary(),
    )


@app.route('/api/config/save', methods=['POST'])
def api_config_save():
    try:
        data = request.json or {}
        updated = config_store.save_config(data)
        logger.info("[App] Saved updated encrypted configuration via UI.")
        activity_log.log_call('success', 'Configuration saved securely via web dashboard')
        usenet_downloader.notify_queue()
        return jsonify({'status': 'success', 'message': 'Configuration saved securely', 'config': updated}), 200
    except Exception as exc:
        logger.error(f"[App] Failed to save configuration: {exc}")
        return jsonify({'status': 'error', 'message': str(exc)}), 500


@app.route('/api/test-connection', methods=['POST'])
def api_test_connection():
    data = request.json or {}
    service = (data.get('service') or '').lower()
    success, message = test_connection(service, data)
    status_code = 200 if success else 400
    status_str = 'success' if success else 'error'
    return jsonify({'status': status_str, 'message': message}), status_code


@app.route('/api/connection-status')
def api_connection_status():
    return jsonify(get_service_connections())


@app.route('/api/plex-status')
def api_plex_status():
    return jsonify(plex_watcher.get_state())


@app.route('/api/poller-status')
def api_poller_status():
    return jsonify(plex_poller.get_state())


@app.route('/api/poller-run', methods=['POST'])
def api_poller_run():
    res = plex_poller.trigger_now()
    return jsonify(res), 200 if res['status'] == 'success' else 400


@app.route('/api/shutdown-now', methods=['POST'])
def api_shutdown_now():
    res = plex_watcher.trigger_shutdown_now()
    return jsonify(res), 200 if res['status'] == 'success' else 400


# ── Built-in Usenet Downloader Endpoints ─────────────────────────────────────

@app.route('/api/downloads-status')
def api_downloads_status():
    return jsonify(usenet_downloader.get_status())


@app.route('/api/downloads/pause', methods=['POST'])
def api_downloads_pause():
    usenet_downloader.pause()
    return jsonify({'status': 'success', 'paused': True}), 200


@app.route('/api/downloads/resume', methods=['POST'])
def api_downloads_resume():
    usenet_downloader.resume()
    return jsonify({'status': 'success', 'paused': False}), 200


@app.route('/api/downloads/<int:download_id>/cancel', methods=['POST'])
def api_downloads_cancel(download_id: int):
    ok = usenet_downloader.cancel_download(download_id)
    return jsonify({'status': 'success' if ok else 'error'}), 200 if ok else 404


@app.route('/api/downloads/<int:download_id>/move', methods=['POST'])
def api_downloads_move(download_id: int):
    direction = ((request.json or {}).get('direction') or '').lower()
    ok = db.move_download(download_id, direction)
    return jsonify({'status': 'success' if ok else 'error'}), 200 if ok else 400


# ── Search & Library Endpoints ───────────────────────────────────────────────

@app.route('/api/search')
def api_search():
    media_type = (request.args.get('type') or 'all').lower()
    query = (request.args.get('q') or '').strip()
    if not query:
        return jsonify({'results': [], 'tv': [], 'movies': []}), 200

    if media_type == 'movie':
        movies = metadata.search_movies(query)
        return jsonify({'results': movies, 'movies': movies, 'tv': []}), 200
    if media_type == 'tv':
        tv = metadata.search_tv(query)
        return jsonify({'results': tv, 'tv': tv, 'movies': []}), 200

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f_movies = ex.submit(metadata.search_movies, query)
        f_tv = ex.submit(metadata.search_tv, query)
        movies = f_movies.result()
        tv = f_tv.result()
    return jsonify({'movies': movies, 'tv': tv, 'results': movies + tv}), 200


@app.route('/api/library')
def api_library():
    movies = db.list_movies()
    prog_map = db.get_all_movies_playback_map()
    for m in movies:
        fpath = m.get('file_path')
        on_disk = bool(fpath and os.path.isfile(fpath)) or (media_manager.find_movie_file_on_disk(m['title'], m.get('year')) is not None)
        m['on_disk'] = on_disk
        if on_disk and (m.get('status') or '').lower() == 'wanted':
            m['status'] = 'downloaded'
        m_prog = prog_map.get(int(m['id'])) if on_disk else None
        res_pos = round(float(m_prog['position_sec']), 1) if m_prog else 0.0
        res_dur = round(float(m_prog['duration_sec']), 1) if m_prog else 0.0
        m['resume_position_sec'] = res_pos
        m['duration_sec'] = res_dur
        m['resume_pct'] = round(min(99.0, res_pos / res_dur * 100.0)) if (res_dur > 0 and res_pos > 15) else 0

    return jsonify({
        'shows': db.list_shows(),
        'movies': movies,
        'recent_downloads': db.list_recent_downloads(15),
    }), 200


@app.route('/api/library/add', methods=['POST'])
def api_library_add():
    data = request.json or {}
    media_type = (data.get('type') or 'tv').lower()
    item_id = data.get('id')
    if not item_id and not data.get('title'):
        return jsonify({'status': 'error', 'message': 'Missing media id'}), 400

    try:
        if media_type == 'movie':
            tmdb_id = int(item_id) if str(item_id or '').isdigit() else None
            imdb_id = data.get('imdb_id') or (str(item_id) if str(item_id or '').startswith('tt') else None)
            res = media_manager.add_movie(
                tmdb_id=tmdb_id,
                title=data.get('title'),
                year=data.get('year'),
                imdb_id=imdb_id,
                poster_url=data.get('poster_url'),
            )
        else:
            auto_queue = bool(data.get('auto_queue', False))
            start_s = int(data.get('start_season') or 1)
            start_e = int(data.get('start_episode') or 1)
            res = media_manager.add_show(int(item_id), auto_queue=auto_queue, start_season=start_s, start_episode=start_e)

        activity_log.log_call(res.get('status', 'info'), res.get('message', 'Media added'))
        code = 200 if res.get('status') in ('success', 'warning') else 400
        return jsonify(res), code
    except Exception as exc:
        logger.error(f"[App] Failed adding media: {exc}")
        return jsonify({'status': 'error', 'message': str(exc)}), 500


@app.route('/api/library/tv/<int:show_id>/episodes')
def api_library_tv_episodes(show_id: int):
    res = media_manager.get_show_episodes_status(show_id)
    code = 200 if res.get('status') == 'success' else 404
    return jsonify(res), code


@app.route('/api/library/tv/<int:show_id>/episode', methods=['POST'])
def api_library_tv_single_episode(show_id: int):
    show = db.get_show(show_id)
    if not show:
        return jsonify({'status': 'error', 'message': 'Show not found'}), 404
    data = request.json or {}
    season = int(data.get('season') or 1)
    episode = int(data.get('episode') or 1)
    ok, msg = media_manager.queue_episode(show, season, episode)
    activity_log.log_call('success' if ok else 'warning', msg)
    return jsonify({'status': 'success' if ok else 'warning', 'message': msg}), 200


@app.route('/api/library/tv/<int:show_id>/queue', methods=['POST'])
def api_library_tv_queue(show_id: int):
    data = request.json or {}
    start_s = int(data.get('start_season') or 1)
    start_e = int(data.get('start_episode') or 1)
    res = media_manager.queue_show_from(show_id, start_season=start_s, start_episode=start_e)
    activity_log.log_call(res.get('status', 'info'), res.get('message', 'Queued show episodes'))
    code = 200 if res.get('status') in ('success', 'warning') else 400
    return jsonify(res), code


@app.route('/api/library/<media_type>/<int:item_id>', methods=['DELETE'])
def api_library_delete(media_type: str, item_id: int):
    if media_type == 'movie':
        ok = db.delete_movie(item_id)
    elif media_type == 'tv':
        ok = db.delete_show(item_id)
    else:
        return jsonify({'status': 'error', 'message': 'Invalid media type'}), 400
    return jsonify({'status': 'success' if ok else 'error'}), 200 if ok else 404


# ── Manual Release Picker, Calendar & Auto-Grab Endpoints ────────────────────

@app.route('/api/releases/movie/<int:movie_id>')
def api_releases_movie(movie_id: int):
    res = media_manager.search_releases_for_movie(movie_id)
    return jsonify(res), 200 if res.get('status') == 'success' else 404


@app.route('/api/releases/tv/<int:show_id>/<int:season>/<int:episode>')
def api_releases_episode(show_id: int, season: int, episode: int):
    res = media_manager.search_releases_for_episode(show_id, season, episode)
    return jsonify(res), 200 if res.get('status') == 'success' else 404


@app.route('/api/releases/grab', methods=['POST'])
def api_releases_grab():
    res = media_manager.grab_manual_release(request.json or {})
    activity_log.log_call(res.get('status', 'info'), res.get('message', 'Manual grab'))
    return jsonify(res), 200 if res.get('status') == 'success' else 400


@app.route('/api/calendar')
def api_calendar():
    return jsonify({'upcoming': media_manager.get_airing_this_week(7)}), 200


@app.route('/api/autograb-run', methods=['POST'])
def api_autograb_run():
    res = media_manager.run_auto_grab()
    return jsonify(res), 200


# ── Built-in Media Player Endpoints ──────────────────────────────────────────

@app.route('/api/player/info/<media_type>/<int:media_id>')
def api_player_info(media_type: str, media_id: int):
    season = request.args.get('season', type=int)
    episode = request.args.get('episode', type=int)
    res = player_service.get_player_info(media_type, media_id, season=season, episode=episode)
    return jsonify(res), 200 if res.get('status') == 'success' else 404


@app.route('/api/player/stream/<media_type>/<int:media_id>')
def api_player_stream(media_type: str, media_id: int):
    season = request.args.get('season', type=int)
    episode = request.args.get('episode', type=int)
    start_sec = request.args.get('start', default=0.0, type=float)
    audio_idx = request.args.get('audio', default=0, type=int)
    force_transcode = request.args.get('transcode', default='0') in ('1', 'true')
    return player_service.stream_media_response(
        media_type=media_type,
        media_id=media_id,
        season=season,
        episode=episode,
        start_sec=start_sec,
        audio_idx=audio_idx,
        force_transcode=force_transcode,
    )


@app.route('/api/player/subtitles/<media_type>/<int:media_id>/<sub_id>.vtt')
def api_player_subtitles(media_type: str, media_id: int, sub_id: str):
    season = request.args.get('season', type=int)
    episode = request.args.get('episode', type=int)
    vtt_text, err = player_service.get_subtitle_vtt(media_type, media_id, sub_id, season=season, episode=episode)
    if err or vtt_text is None:
        return Response(err or 'Subtitle not found', status=404, mimetype='text/plain')
    return Response(vtt_text, status=200, mimetype='text/vtt; charset=utf-8')


@app.route('/api/player/heartbeat', methods=['POST'])
def api_player_heartbeat():
    res = player_service.record_heartbeat(request.json or {})
    return jsonify(res), 200 if res.get('status') == 'success' else 400


@app.route('/api/player/watched', methods=['POST'])
def api_player_watched():
    data = request.json or {}
    media_type = (data.get('media_type') or '').lower()
    media_id = int(data.get('media_id') or 0)
    season = int(data['season']) if data.get('season') is not None else None
    episode = int(data['episode']) if data.get('episode') is not None else None
    if not media_type or not media_id:
        return jsonify({'status': 'error', 'message': 'Missing media_type or media_id'}), 400
    res = player_service.mark_media_watched(media_type, media_id, season=season, episode=episode)
    return jsonify(res), 200 if res.get('status') == 'success' else 400


# ── Activity & Disk Endpoints ────────────────────────────────────────────────

@app.route('/api/clear-logs', methods=['POST'])
def api_clear_logs():
    activity_log.clear()
    logger.info("Activity history cleared via UI.")
    return jsonify({"status": "success"}), 200


@app.route('/api/activity')
def api_activity():
    return jsonify(activity_log.get_entries()), 200


@app.route('/api/disk-space')
def api_disk_space():
    return jsonify(get_disk_space_summary()), 200


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
