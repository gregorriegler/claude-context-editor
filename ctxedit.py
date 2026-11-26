#!/usr/bin/env python3
"""ctxedit: edit the context of a Claude Code session by hand, save as a fork.

Usage:
  ctxedit                 open the session picker
  ctxedit <session-id>    open that session
  ctxedit <path.jsonl>    open that file
"""
import argparse
import copy
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import ctxcore  # noqa: E402
import ctxsuggest  # noqa: E402

RUNNING_WINDOW = 60  # seconds: a session touched this recently may still be running
UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)


def config_dir():
    return os.environ.get('CLAUDE_CONFIG_DIR') or os.path.join(os.path.expanduser('~'), '.claude')


def projects_root():
    return os.path.join(config_dir(), 'projects')


def app_dir():
    return os.path.join(os.path.expanduser('~'), '.ctxedit')


def project_dir_name(cwd):
    return re.sub(r'[^A-Za-z0-9]', '-', cwd)


# ------------------------------------------------------------------ picker

class Summaries(object):
    """First prompt, step count and token count per session file.

    Costs a full parse, so results are cached by (path, mtime, size) in
    ~/.ctxedit/cache.json. Nothing is cached inside ~/.claude.
    """

    def __init__(self):
        self.path = os.path.join(app_dir(), 'cache.json')
        self.lock = threading.Lock()
        try:
            with open(self.path, encoding='utf-8') as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}
        self.dirty = False

    def get(self, path):
        st = os.stat(path)
        key = '%s|%d|%d' % (path, int(st.st_mtime * 1000), st.st_size)
        with self.lock:
            hit = self.data.get(path)
            if hit and hit.get('key') == key:
                return hit['summary']
        s = ctxcore.Session(path)
        live = [x for x in s.steps if x.group == 'live']
        prompt = ' '.join((s.first_prompt or '').split())
        summary = {
            'prompt': prompt[:80] + ('…' if len(prompt) > 80 else ''),
            'title': s.title, 'steps': len(live), 'tokens': sum(x.tokens for x in live), 'cwd': s.cwd,
        }
        with self.lock:
            self.data[path] = {'key': key, 'summary': summary}
            self.dirty = True
        return summary

    def cached(self, path, mtime, size):
        key = '%s|%d|%d' % (path, int(mtime * 1000), size)
        hit = self.data.get(path)
        return hit['summary'] if hit and hit.get('key') == key else None

    def flush(self):
        with self.lock:
            if not self.dirty:
                return
            live = {p: v for p, v in self.data.items() if os.path.exists(p)}
            try:
                os.makedirs(app_dir(), exist_ok=True)
                tmp = self.path + '.tmp'
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(live, f)
                os.replace(tmp, self.path)
                self.dirty = False
            except OSError:
                pass


def list_projects(summaries):
    root = projects_root()
    projects = []
    if not os.path.isdir(root):
        return projects
    for name in os.listdir(root):
        folder = os.path.join(root, name)
        if not os.path.isdir(folder):
            continue
        sessions = []
        for fn in os.listdir(folder):
            if not fn.endswith('.jsonl'):
                continue
            path = os.path.join(folder, fn)
            try:
                st = os.stat(path)
            except OSError:
                continue
            sessions.append({'id': fn[:-6], 'path': path, 'mtime': st.st_mtime, 'size': st.st_size,
                             'summary': summaries.cached(path, st.st_mtime, st.st_size)})
        if not sessions:
            continue
        sessions.sort(key=lambda x: -x['mtime'])
        cwd = next((x['summary']['cwd'] for x in sessions if x['summary'] and x['summary'].get('cwd')), None)
        projects.append({'dir': name, 'name': cwd or name, 'sessions': sessions, 'mtime': sessions[0]['mtime']})
    projects.sort(key=lambda p: -p['mtime'])
    return projects


def preselect_for_cwd(cwd):
    """Project folder for the directory ctxedit was started in (or a parent)."""
    root = projects_root()
    cur = os.path.abspath(cwd)
    while True:
        name = project_dir_name(cur)
        if os.path.isdir(os.path.join(root, name)):
            return name
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def resolve_target(arg):
    """A session id or a path from the command line -> absolute file path."""
    if os.path.isfile(arg):
        return os.path.abspath(arg)
    if UUID_RE.match(arg):
        root = projects_root()
        if os.path.isdir(root):
            for name in os.listdir(root):
                path = os.path.join(root, name, arg + '.jsonl')
                if os.path.isfile(path):
                    return path
        raise SystemExit('ctxedit: no session %s under %s' % (arg, root))
    raise SystemExit('ctxedit: %s is neither a file nor a session id' % arg)


# --------------------------------------------------------------------- app

class App(object):
    def __init__(self, start_cwd, direct=None, suggest_model=None):
        self.token = secrets.token_urlsafe(24)
        self.summaries = Summaries()
        self.start_cwd = start_cwd
        self.direct = direct
        self.suggest_model = suggest_model
        self.docs = {}
        self.lock = threading.RLock()
        self.port = None
        self.last_request = time.time()

    # every session file the server may open: under the projects root, or
    # the one named on the command line
    def allowed(self, path):
        real = os.path.realpath(path)
        root = os.path.realpath(projects_root())
        if self.direct and real == os.path.realpath(self.direct):
            return True
        return real.endswith('.jsonl') and os.path.commonpath([real, root]) == root and os.path.isfile(real)

    def doc(self, doc_id):
        d = self.docs.get(doc_id)
        if not d:
            raise ApiError(404, 'this editor tab is stale: reopen the session')
        return d

    # -- handlers ----------------------------------------------------------
    def api_sessions(self, q, body):
        projects = list_projects(self.summaries)
        return {'projects': projects, 'preselect': preselect_for_cwd(self.start_cwd),
                'direct': self.direct, 'root': projects_root(), 'now': time.time()}

    def api_ping(self, q, body):
        return {'ok': True}  # the open page calls this, which keeps --idle-exit from firing

    def api_summary(self, q, body):
        path = q.get('path', [''])[0]
        if not self.allowed(path):
            raise ApiError(403, 'not a session file')
        out = self.summaries.get(path)
        self.summaries.flush()
        return out

    def api_open(self, q, body):
        path = body.get('path') or ''
        if not self.allowed(path):
            raise ApiError(403, 'not a session file')
        s = ctxcore.Session(path)  # read once; this is the snapshot
        ed = ctxcore.Editor(s)
        doc_id = secrets.token_hex(8)
        with self.lock:
            if len(self.docs) > 8:
                self.docs.pop(next(iter(self.docs)))
            self.docs[doc_id] = {'session': s, 'editor': ed, 'lock': threading.RLock()}
        age = time.time() - s.mtime
        sub_dir = os.path.join(os.path.dirname(s.path), s.session_id, 'subagents')
        subagents = len([f for f in os.listdir(sub_dir) if f.endswith('.jsonl')]) if os.path.isdir(sub_dir) else 0
        return {
            'doc': doc_id,
            'meta': {'id': s.session_id, 'path': s.path, 'title': s.title or s.first_prompt[:80], 'cwd': s.cwd,
                     'version': s.version, 'mtime': s.mtime, 'compacted': bool(s.boundary),
                     'maybe_running': age < RUNNING_WINDOW, 'age': age, 'bad_lines': s.bad_lines,
                     'bookkeeping': s.other_counts(), 'subagent_files': subagents, 'marker': ed.marker,
                     'trim_marker': ctxcore.TRIM_MARKER},
            'steps': [st.to_dict(full=False, preview=400) for st in s.steps],
            'counters': ed.counters(),
        }

    def api_detail(self, q, body):
        d = self.doc(q.get('doc', [''])[0])
        sid = q.get('id', [''])[0]
        if sid not in d['session'].step_by_id:
            raise ApiError(404, 'no such step')
        with d['lock']:
            return d['editor'].detail(sid)

    def _changed(self, d, ids):
        ed = d['editor']
        return {'changed': [ed.view(i) for i in ids], 'counters': ed.counters()}

    def api_op(self, q, body):
        d = self.doc(body.get('doc'))
        with d['lock']:
            try:
                ids = d['editor'].apply(body.get('op') or {})
            except (ValueError, KeyError, TypeError) as ex:
                raise ApiError(400, str(ex))
            return self._changed(d, ids)

    def api_undo(self, q, body):
        d = self.doc(body.get('doc'))
        with d['lock']:
            return self._changed(d, d['editor'].undo())

    def api_redo(self, q, body):
        d = self.doc(body.get('doc'))
        with d['lock']:
            return self._changed(d, d['editor'].redo())

    def api_settings(self, q, body):
        d = self.doc(body.get('doc'))
        with d['lock']:
            ed = d['editor']
            if 'marker' in body:
                ed.marker = bool(body['marker'])
            ids = [i for i, st in ed.state.items() if st.get('removed') or st.get('media')]
            out = self._changed(d, ids)
            out['marker'] = ed.marker
            return out

    def api_search(self, q, body):
        d = self.doc(q.get('doc', [''])[0])
        needle = q.get('q', [''])[0].lower()
        ed = d['editor']
        if not needle:
            return {'ids': None}
        ids = []
        with d['lock']:
            for st in d['session'].steps:
                hay = ed.effective_text(st.id) if st.id in ed.state and st.editable else st.text
                if needle in hay.lower() or (st.tool and needle in st.tool.lower()) or (st.note and needle in st.note.lower()):
                    ids.append(st.id)
        return {'ids': ids}

    def api_check(self, q, body):
        d = self.doc(body.get('doc'))
        with d['lock']:
            errors, warnings = ctxcore.check(d['editor'])
        return {'errors': errors, 'warnings': warnings}

    def api_save(self, q, body):
        d = self.doc(body.get('doc'))
        with d['lock']:
            out = ctxcore.save(d['editor'], edits_dir=os.path.join(app_dir(), 'edits'))
            out['counters'] = d['editor'].counters()
        return out

    def _snapshot(self, d):
        """A private copy of the edit state, so a slow model call holds no lock."""
        with d['lock']:
            ed = d['editor']
            snapshot = ctxcore.Editor(d['session'], marker=ed.marker, lock_thinking=ed.lock_thinking)
            snapshot.state = copy.deepcopy(ed.state)
        return snapshot

    def api_suggest(self, q, body):
        snapshot = self._snapshot(self.doc(body.get('doc')))
        try:
            return ctxsuggest.suggest(snapshot, full=bool(body.get('full')), model=self.suggest_model)
        except RuntimeError as ex:
            raise ApiError(502, str(ex))

    def api_detours(self, q, body):
        snapshot = self._snapshot(self.doc(body.get('doc')))
        try:
            return ctxsuggest.find_detours(snapshot, model=self.suggest_model)
        except RuntimeError as ex:
            raise ApiError(502, str(ex))

    def api_detour_range(self, q, body):
        """The same facts for a range the user moved by hand."""
        d = self.doc(body.get('doc'))
        try:
            first, last = int(body.get('first')), int(body.get('last'))
        except (TypeError, ValueError):
            raise ApiError(400, 'first and last required')
        if first > last:
            raise ApiError(400, 'empty range')
        with d['lock']:
            return ctxsuggest.resolve_detour(d['editor'], first, last)


class ApiError(Exception):
    def __init__(self, status, msg):
        Exception.__init__(self, msg)
        self.status = status


GET_ROUTES = {'sessions', 'summary', 'detail', 'search', 'ping'}
POST_ROUTES = {'open', 'op', 'undo', 'redo', 'settings', 'check', 'save', 'suggest', 'detours', 'detour_range'}


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'ctxedit'
        protocol_version = 'HTTP/1.1'

        def log_message(self, fmt, *args):
            pass

        def _send(self, status, body, ctype='application/json; charset=utf-8'):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.end_headers()
            self.wfile.write(body)

        def _authorised(self, query):
            # Host check stops DNS rebinding; the token stops other pages.
            host = (self.headers.get('Host') or '').lower()
            if host not in ('127.0.0.1:%d' % app.port, 'localhost:%d' % app.port):
                return False
            given = self.headers.get('X-Ctxedit-Token') or (query.get('t') or [''])[0]
            return hmac.compare_digest(given.encode(), app.token.encode())

        def _handle(self, method):
            url = urlparse(self.path)
            query = parse_qs(url.query)
            if not self._authorised(query):
                return self._send(403, {'error': 'missing or wrong token'})
            app.last_request = time.time()
            if method == 'GET' and url.path == '/':
                with open(os.path.join(HERE, 'index.html'), 'rb') as f:
                    return self._send(200, f.read(), 'text/html; charset=utf-8')
            if not url.path.startswith('/api/'):
                return self._send(404, {'error': 'not found'})
            name = url.path[len('/api/'):]
            if name not in (GET_ROUTES if method == 'GET' else POST_ROUTES):
                return self._send(404, {'error': 'not found'})
            body = {}
            if method == 'POST':
                try:
                    n = int(self.headers.get('Content-Length') or 0)
                    body = json.loads(self.rfile.read(n) or b'{}')
                    if not isinstance(body, dict):
                        raise ValueError
                except ValueError:
                    return self._send(400, {'error': 'bad request body'})
            try:
                out = getattr(app, 'api_' + name)(query, body)
            except ApiError as ex:
                return self._send(ex.status, {'error': str(ex)})
            except Exception as ex:  # keep the server alive, tell the page
                return self._send(500, {'error': '%s: %s' % (type(ex).__name__, ex)})
            self._send(200, out)

        def do_GET(self):
            self._handle('GET')

        def do_POST(self):
            self._handle('POST')

    return Handler


def open_browser(url):
    """Best effort. On WSL, hand the URL to Windows."""
    try:
        with open('/proc/version') as f:
            wsl = 'microsoft' in f.read().lower()
    except OSError:
        wsl = False
    if wsl:
        for cmd in (['wslview', url], ['cmd.exe', '/c', 'start', '', url.replace('&', '^&')],
                    ['powershell.exe', '-NoProfile', '-Command', 'Start-Process', "'%s'" % url]):
            if shutil.which(cmd[0]):
                try:
                    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     cwd='/mnt/c' if os.path.isdir('/mnt/c') else '/')
                    return True
                except OSError:
                    continue
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def detach(argv):
    """Start the same command again as a background process of its own and return.

    The child is cut loose from the terminal and from the process that called
    us (a new session on Linux/macOS, a detached process on Windows), so it
    keeps running after the caller is gone. Its output goes to a private log
    file. We wait until it reports its URL, or until it has failed.
    """
    fd, log = tempfile.mkstemp(prefix='ctxedit-', suffix='.log')
    env = dict(os.environ, CTXEDIT_DETACHED='1', PYTHONUNBUFFERED='1')
    kw = {}
    if os.name == 'nt':
        kw['creationflags'] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw['start_new_session'] = True
    with os.fdopen(fd, 'w') as out:
        child = subprocess.Popen([sys.executable, os.path.abspath(__file__)] + list(argv),
                                 stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                 env=env, **kw)
    deadline = time.time() + 15
    text = ''
    while time.time() < deadline:
        with open(log, encoding='utf-8', errors='replace') as f:
            text = f.read()
        m = re.search(r'http://\S+', text)
        if m:
            print('ctxedit is running in the background at\n  %s\npid %d, log: %s' % (m.group(0), child.pid, log))
            return 0
        if child.poll() is not None:
            break
        time.sleep(0.1)
    if child.poll() is None:
        child.kill()
    print('ctxedit failed to start in the background:\n%s' % (text.strip() or '(no output)'))
    try:
        os.unlink(log)
    except OSError:
        pass
    return 1


def main(argv=None):
    ap = argparse.ArgumentParser(prog='ctxedit', description='Edit the context of a Claude Code session and save it as a fork.')
    ap.add_argument('target', nargs='?', help='session id or path to a .jsonl session file')
    ap.add_argument('--no-browser', action='store_true', help='print the URL instead of opening a browser')
    ap.add_argument('--port', type=int, default=0, help='port to bind on 127.0.0.1 (default: any free port)')
    ap.add_argument('--suggest-model', default=os.environ.get('CTXEDIT_SUGGEST_MODEL', 'sonnet'),
                    help='model for "Suggest cuts" (default: sonnet)')
    ap.add_argument('--idle-exit', type=float, default=0, metavar='MINUTES',
                    help='stop by itself after this many minutes without a request (for background use)')
    ap.add_argument('--detach', action='store_true',
                    help='start in the background, print the URL and return')
    args = ap.parse_args(argv)
    if args.detach and not os.environ.get('CTXEDIT_DETACHED'):
        return detach(sys.argv[1:] if argv is None else argv)

    direct = resolve_target(args.target) if args.target else None
    app = App(os.getcwd(), direct=direct, suggest_model=args.suggest_model)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(app))
    server.daemon_threads = True
    app.port = server.server_address[1]
    url = 'http://127.0.0.1:%d/?t=%s' % (app.port, app.token)
    print('ctxedit is running at\n  %s' % url, flush=True)
    if not args.detach:
        print('Press Ctrl+C to stop.', flush=True)
    if not args.no_browser:
        open_browser(url)
    if args.idle_exit > 0:
        def watchdog():
            while time.time() - app.last_request < args.idle_exit * 60:
                time.sleep(5)
            print('idle for %g minutes, stopping' % args.idle_exit, flush=True)
            server.shutdown()
        threading.Thread(target=watchdog, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nstopped')
    finally:
        app.summaries.flush()
        server.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
