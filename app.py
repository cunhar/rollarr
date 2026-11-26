from flask import Flask, request, jsonify, render_template, send_from_directory

import os
import logging
import threading
from logging.handlers import RotatingFileHandler
import concurrent.futures

try:
    threading.stack_size(2 * 1024 * 1024)
except Exception:
    pass

import config_store
from integrations import metadata
from services import db, plex_watcher, plex_poller, activity_log, usenet_downloader, media_manager
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

@app.route('/manifest.json')
def pwa_manifest():
    return send_from_directory(app.static_folder, 'manifest.json', mimetype='application/manifest+json')


@app.route('/sw.js')
def pwa_service_worker():
    resp = send_from_directory(app.static_folder, 'sw.js', mimetype='application/javascript')
    resp.headers['Service-Worker-Allowed'] = '/'
    resp.headers['Cache-Control'] = 'no-cache'
    return resp


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
    ok = usenet_downloader.move_download(download_id, direction)
    return jsonify({'status': 'success' if ok else 'error'}), 200 if ok else 400


# ── Search & Library Endpoints ───────────────────────────────────────────────

def _annotate_search_with_library(movies: list, tv: list):
    """Attach library_id to search results that are already tracked in SQLite."""
    if tv:
        tracked_shows = db.list_shows()
        by_tvmaze = {int(s['tvmaze_id']): s['id'] for s in tracked_shows if s.get('tvmaze_id')}
        by_title = {media_manager.clean_title(s['title']): s['id'] for s in tracked_shows if s.get('title')}
        for item in tv:
            tid = item.get('tvmaze_id') or item.get('id')
            lid = by_tvmaze.get(int(tid)) if str(tid or '').lstrip('-').isdigit() else None
            if not lid and item.get('title'):
                lid = by_title.get(media_manager.clean_title(item['title']))
            if lid:
                item['library_id'] = lid

    if movies:
        tracked_movies = db.list_movies()
        by_tmdb = {int(m['tmdb_id']): m['id'] for m in tracked_movies if m.get('tmdb_id')}
        by_imdb = {str(m['imdb_id']).strip(): m['id'] for m in tracked_movies if m.get('imdb_id')}
        by_title = {media_manager.clean_title(m['title']): m['id'] for m in tracked_movies if m.get('title')}
        for item in movies:
            tmid = item.get('tmdb_id') or item.get('id')
            lid = by_tmdb.get(int(tmid)) if str(tmid or '').lstrip('-').isdigit() else None
            if not lid and item.get('imdb_id'):
                lid = by_imdb.get(str(item['imdb_id']).strip())
            if not lid and item.get('title'):
                lid = by_title.get(media_manager.clean_title(item['title']))
            if lid:
                item['library_id'] = lid


@app.route('/api/search')
def api_search():
    media_type = (request.args.get('type') or 'all').lower()
    query = (request.args.get('q') or '').strip()
    if not query:
        return jsonify({'results': [], 'tv': [], 'movies': []}), 200

    if media_type == 'movie':
        movies = metadata.search_movies(query)
        _annotate_search_with_library(movies, [])
        return jsonify({'results': movies, 'movies': movies, 'tv': []}), 200
    if media_type == 'tv':
        tv = metadata.search_tv(query)
        _annotate_search_with_library([], tv)
        return jsonify({'results': tv, 'tv': tv, 'movies': []}), 200

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f_movies = ex.submit(metadata.search_movies, query)
        f_tv = ex.submit(metadata.search_tv, query)
        movies = f_movies.result()
        tv = f_tv.result()
    _annotate_search_with_library(movies, tv)
    return jsonify({'movies': movies, 'tv': tv, 'results': movies + tv}), 200


@app.route('/api/library')
def api_library():
    movies = media_manager.sync_on_disk_movies()
    for m in movies:
        fpath = m.get('file_path')
        disk_path = fpath if (fpath and os.path.isfile(fpath)) else media_manager.find_movie_file_on_disk(m['title'], m.get('year'))
        cur_status = (m.get('status') or 'wanted').lower()
        if disk_path:
            if cur_status != 'downloaded':
                m['status'] = 'downloaded'
                m['file_path'] = disk_path
                db.update_movie_status(m['id'], 'downloaded', file_path=disk_path)
        elif db.has_active_download('movie', m['title']):
            if cur_status != 'downloading':
                m['status'] = 'downloading'
                db.update_movie_status(m['id'], 'downloading')
        elif cur_status == 'downloading':
            # Recover movies stuck in 'downloading' when no active download job exists
            m['status'] = 'wanted'
            db.update_movie_status(m['id'], 'wanted')

    status_rank = {'downloaded': 0, 'downloading': 1, 'wanted': 2}
    movies.sort(key=lambda m: (
        status_rank.get((m.get('status') or 'wanted').lower(), 3),
        (m.get('title') or '').lower(),
    ))

    active_dls = db.list_active_downloads()
    active_show_titles = {
        media_manager.clean_title(d['title'])
        for d in active_dls
        if d.get('media_type') == 'episode' and d.get('title')
    }
    shows = media_manager.sync_on_disk_shows()
    for s in shows:
        on_disk_cnt = len(media_manager._scan_show_episodes_on_disk(s['title']))
        s['on_disk_count'] = on_disk_cnt
        is_dl = media_manager.clean_title(s['title']) in active_show_titles
        lw_s = s.get('last_watched_season')
        lw_e = s.get('last_watched_episode')
        if is_dl:
            s['status'] = 'downloading'
        elif on_disk_cnt > 0:
            s['status'] = f"{on_disk_cnt} on disk"
        elif lw_s is not None and lw_e is not None:
            s['status'] = f"caught up (S{int(lw_s):02d}E{int(lw_e):02d})"
        else:
            s['status'] = 'idle'

    show_rank = lambda s: (
        0 if s['status'] == 'downloading' else (
            1 if s.get('on_disk_count', 0) > 0 else (
                2 if str(s.get('status') or '').startswith('caught up') else 3
            )
        ),
        (s.get('title') or '').lower(),
    )
    shows.sort(key=show_rank)

    return jsonify({
        'shows': shows,
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


@app.route('/api/library/tv/<int:show_id>/episode', methods=['POST', 'DELETE'])
def api_library_tv_single_episode(show_id: int):
    show = db.get_show(show_id)
    if not show:
        return jsonify({'status': 'error', 'message': 'Show not found'}), 404
    data = request.json or {}
    season = int(data.get('season') or request.args.get('season') or 1)
    episode = int(data.get('episode') or request.args.get('episode') or 1)
    if request.method == 'DELETE':
        res = media_manager.manual_delete_episode(show_id, season, episode)
        activity_log.log_call(res.get('status', 'info'), res.get('message', 'Deleted episode'))
        return jsonify(res), 200 if res.get('status') in ('success', 'warning') else 400
    ok, msg = media_manager.queue_episode(show, season, episode)
    activity_log.log_call('success' if ok else 'warning', msg)
    return jsonify({'status': 'success' if ok else 'warning', 'message': msg}), 200


@app.route('/api/library/tv/<int:show_id>/season/<int:season>', methods=['DELETE'])
def api_library_tv_delete_season(show_id: int, season: int):
    res = media_manager.manual_delete_season(show_id, season)
    activity_log.log_call(res.get('status', 'info'), res.get('message', 'Deleted season'))
    return jsonify(res), 200 if res.get('status') in ('success', 'warning') else 404


@app.route('/api/library/tv/<int:show_id>/queue', methods=['POST'])
def api_library_tv_queue(show_id: int):
    data = request.json or {}
    start_s = int(data.get('start_season') or 1)
    start_e = int(data.get('start_episode') or 1)
    res = media_manager.queue_show_from(show_id, start_season=start_s, start_episode=start_e)
    activity_log.log_call(res.get('status', 'info'), res.get('message', 'Queued show episodes'))
    code = 200 if res.get('status') in ('success', 'warning') else 400
    return jsonify(res), code


@app.route('/api/library/movie/<int:movie_id>/queue', methods=['POST'])
def api_library_movie_queue(movie_id: int):
    movie = db.get_movie(movie_id)
    if not movie:
        return jsonify({'status': 'error', 'message': 'Movie not found'}), 404
    ok, msg = media_manager.queue_movie(movie)
    activity_log.log_call('success' if ok else 'warning', msg)
    return jsonify({'status': 'success' if ok else 'warning', 'message': msg}), 200


@app.route('/api/library/<media_type>/<int:item_id>', methods=['DELETE'])
def api_library_delete(media_type: str, item_id: int):
    if media_type == 'movie':
        mov = db.get_movie(item_id)
        if mov:
            for d in db.list_active_downloads():
                if d.get('media_type') == 'movie' and (d.get('media_id') == item_id or media_manager.clean_title(d.get('title') or '') == media_manager.clean_title(mov['title'])):
                    usenet_downloader.cancel_download(int(d['id']))
            deleted_disk, detail = media_manager.delete_movie_file(mov['title'], mov.get('year'), reported_path=mov.get('file_path'))
            usenet_downloader._notify_media_servers()
            activity_log.log_call('info', f"Removed movie '{mov['title']}' ({detail})")
        ok = db.delete_movie(item_id)
    elif media_type == 'tv':
        show = db.get_show(item_id)
        if show:
            for d in db.list_active_downloads():
                if d.get('media_type') == 'episode' and (d.get('media_id') == item_id or media_manager.clean_title(d.get('title') or '') == media_manager.clean_title(show['title'])):
                    usenet_downloader.cancel_download(int(d['id']))
            _, detail = media_manager.delete_show_directory(show['title'])
            usenet_downloader._notify_media_servers()
            activity_log.log_call('info', f"Removed TV show '{show['title']}' ({detail})")
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


@app.route('/api/releases/blocklist', methods=['POST', 'DELETE'])
def api_releases_blocklist():
    data = request.json or {}
    nzb_guid = (data.get('nzb_guid') or '').strip()
    nzb_title = (data.get('nzb_title') or '').strip() or nzb_guid
    if not nzb_guid:
        return jsonify({'status': 'error', 'message': 'Missing nzb_guid'}), 400

    if request.method == 'DELETE' or data.get('blocklisted') is False:
        db.remove_from_blocklist(nzb_guid)
        msg = f"Unblocklisted release '{nzb_title}'"
        activity_log.log_call('info', msg)
        return jsonify({'status': 'success', 'blocklisted': False, 'message': msg}), 200

    reason = (data.get('reason') or 'Manually blocklisted by user').strip()
    db.add_to_blocklist(nzb_guid, nzb_title, reason)
    msg = f"Blocklisted release '{nzb_title}'"
    activity_log.log_call('warning', msg)
    return jsonify({'status': 'success', 'blocklisted': True, 'message': msg}), 200


@app.route('/api/calendar')
def api_calendar():
    return jsonify({'upcoming': media_manager.get_airing_this_week(7)}), 200


@app.route('/api/autograb-run', methods=['POST'])
def api_autograb_run():
    res = media_manager.run_auto_grab()
    return jsonify(res), 200


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
