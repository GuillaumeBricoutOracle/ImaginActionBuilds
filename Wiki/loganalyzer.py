#!/usr/bin/env python3
"""Analyseur du log scénario (Logs/scenario_console.txt).

Parse le fichier écrit par ScenarioFileLogger (Unity) et maintient des
statistiques : sessions, boucles, events, fiabilité ESP, audio, erreurs.
Conçu pour un suivi incrémental (tail) : parse tout au démarrage puis
`poll()` ne lit que les octets ajoutés.

Testable en standalone :  python loganalyzer.py [chemin_du_log]
"""

import io
import json
import os
import re
import statistics
import sys
import threading
import time
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta

# ─── Regex des lignes connues ────────────────────────────────────────────────
RE_SESSION      = re.compile(r'^=+ SESSION (\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) =+')
RE_SESSION_END  = re.compile(r'^=+ FIN SESSION (\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) \((\d+) boucle')
RE_LOOP         = re.compile(r'^-+ BOUCLE #(\d+) [—-]+ (\d{2}:\d{2}:\d{2})')
RE_LINE         = re.compile(r'^(\d{2}):(\d{2}):(\d{2})\.(\d{3}) \[(\w+) *\] (.*)$')

# (?:[^\x00-\x7F]+\s+)? : tolère un préfixe emoji ajouté pour la console Unity
RE_FIRE_EVENT   = re.compile(r'^(?:[^\x00-\x7F]+\s+)?[\d,.]+ -- FIRE EVENT : (\S+)')
RE_START_SCENAR = re.compile(r'-- START SCENARIO(?:\s*:\s*(.+))?$')
RE_STOP_SCENAR  = re.compile(r'-- STOP SCENARIO')
RE_WS_OUT       = re.compile(r'^\[WS OUT\] (?:[^\x00-\x7F]+\s+)?(\S+) \(([\d.]+):(\d+)\) => (.*)$')
RE_WS_OUT_ACT   = re.compile(r'"action"\s*:\s*"([^"]*)"')
RE_CONNECTED    = re.compile(r'Connecté :\s+(?:[^\x00-\x7F]+\s+)?(\S+) \(ws://([\d.]+):(\d+)\S*\) en (\d+) ms')
RE_CONN_FAILED  = re.compile(r'Connexion échouée vers (\S+) \(([\d.]+):(\d+)\) : (.+?)(?: [—-]+ |$)')
RE_ACTION_LOST  = re.compile(r'Socket non ouverte pour (\S+) \(([\d.]+):(\d+)\).*Action perdue')
RE_SOUND_OK     = re.compile(r"\[Son\]\s+(?:[^\x00-\x7F]+\s+)?Lecture OK : '(.+?)' sur channel '(.+?)'")
RE_SOUND_ERR    = re.compile(r'\[Son\] (Fichier audio introuvable|Erreur chargement audio|Clip null)')
RE_BUTTON       = re.compile(r'\[ControllerManager\]\[IN\]\s+(?:[^\x00-\x7F]+\s+)?button=(\S+)')
RE_WS_IN        = re.compile(r'\[WS IN\]')
RE_WS_IN_NAMED  = re.compile(r'\[WS IN\](?:</color>)?\s+(\S+) <= ')
RE_WS_IN_SUPPR  = re.compile(r'(\d+) message\(s\) non affiché')

MAX_RECENT_ERRORS = 300
MAX_RECENT_EVENTS = 200
MAX_RECENT_LOOPS  = 100     # pour le résumé live (vue d'ensemble)
MAX_RANGE_LOOPS   = 5000    # pour l'analyse d'une période : on veut tout voir
MAX_RECORDS       = 800_000   # ~1 semaine de nuits chargées (~450 Mo au plafond) ;
                              # au-delà les plus vieux sont purgés -> les stats
                              # d'incidents/activité d'une période plus ancienne
                              # ne sont plus disponibles (le dashboard le signale).


class LogAnalyzer:
    def __init__(self, log_path):
        self.log_path = log_path
        self.lock = threading.Lock()
        self._pos = 0
        self._buffer = ''
        self._archives_loaded = set()
        self.reset()

    # ─── État ────────────────────────────────────────────────────────────────
    def reset(self):
        self.sessions = []          # [{start, end, crashed, loops:[...], counts:{}, scenario_starts}]
        self.cur_session = None
        self.cur_date = None        # datetime.date courante (déduite des en-têtes)
        self.last_dt = None         # datetime de la dernière ligne horodatée
        self.last_line_wall = None  # time.time() de la dernière ligne lue

        self.esp = defaultdict(lambda: {
            'ws_out': 0, 'actions': Counter(), 'lost': 0, 'lost_last': None,
            'connects': 0, 'connect_ms': [], 'connect_ms_last': None,
            'failures': 0, 'failure_last_cause': None, 'failure_last': None,
            'last_out': None, 'last_out_action': None, 'ip': None,
            'ws_in': 0, 'last_in': None,
        })
        self.event_stats = defaultdict(lambda: {'count': 0, 'durations': []})
        self.recent_events = deque(maxlen=MAX_RECENT_EVENTS)   # {ts, id}
        self._last_event = None      # (dt, id) pour calculer la durée

        self.recent_errors = deque(maxlen=MAX_RECENT_ERRORS)   # {ts, level, msg}
        self.level_per_hour = defaultdict(Counter)             # 'YYYY-MM-DD HH' -> {level: n}

        self.sound_plays = 0
        self.sound_by_channel = Counter()
        self.sound_files = Counter()
        self.sound_errors = 0
        self.buttons = Counter()
        self.ws_in_shown = 0
        self.ws_in_suppressed = 0
        self.total_lines = 0

        # Enregistrements horodatés (epoch, kind, a, b) pour les stats par plage
        # de dates / par spectacle. Kinds : fire, evdur, ws_out, lost, connect,
        # fail, sound, sound_err, button, err.
        self.records = []

        # Spectacles = itérations de la boucle du scénario. Chaque entrée :
        # {n, start(epoch), end(epoch|None), duration_s, day, partial, scenario}
        self.shows = []
        self._open_show = None
        self._current_scenario = None
        self._scenario_running = False   # entre START et STOP SCENARIO

        # Registre global des signatures de messages d'erreur/warning : sert à
        # la détection de nouveauté (premier signalement d'un message inédit).
        self.error_signatures = {}

    # ─── Datation ────────────────────────────────────────────────────────────
    def _make_dt(self, h, m, s, ms):
        """Timestamp complet à partir de HH:mm:ss.fff + date courante,
        avec bascule de jour quand l'heure recule (passage de minuit)."""
        if self.cur_date is None:
            self.cur_date = datetime.now().date()
        dt = datetime.combine(self.cur_date, datetime.min.time()).replace(
            hour=h, minute=m, second=s, microsecond=ms * 1000)
        if self.last_dt is not None and dt < self.last_dt - timedelta(hours=12):
            self.cur_date = self.cur_date + timedelta(days=1)
            dt += timedelta(days=1)
        self.last_dt = dt
        return dt

    def _rec(self, dt, kind, a=None, b=None):
        # Interne les chaînes répétitives (noms d'ESP, ids d'events, actions,
        # messages types) : elles reviennent des milliers de fois -> un seul
        # objet partagé au lieu d'un doublon par record. Divise la RAM des
        # records par ~4-5 sur un gros log.
        if type(a) is str:
            a = sys.intern(a)
        if type(b) is str and len(b) <= 64:
            b = sys.intern(b)
        self.records.append((dt.timestamp(), kind, a, b))
        if len(self.records) > MAX_RECORDS:
            del self.records[:MAX_RECORDS // 8]

    # ─── Spectacles (une boucle du scénario = un spectacle) ─────────────────
    def _start_show(self, dt, n):
        self._open_show = {'n': n, 'start': dt.timestamp(), 'end': None,
                           'duration_s': None, 'day': dt.strftime('%Y-%m-%d'),
                           'partial': False, 'scenario': self._current_scenario}

    def _close_show(self, dt, partial=False):
        if self._open_show is None:
            return
        show = self._open_show
        show['end'] = dt.timestamp() if dt else show['start']
        show['duration_s'] = round(show['end'] - show['start'], 1)
        show['partial'] = partial
        # Un « spectacle » d'une poignée de secondes = un faux départ, on l'ignore
        if show['duration_s'] >= 5:
            self.shows.append(show)
            if len(self.shows) > 20000:      # ~1 an à 50 spectacles/jour
                del self.shows[:2000]
        self._open_show = None

    # ─── Parsing ─────────────────────────────────────────────────────────────
    def feed_line(self, raw):
        line = raw.rstrip('\r\n')
        if not line:
            return
        self.total_lines += 1
        self.last_line_wall = time.time()

        m = RE_SESSION.match(line)
        if m:
            self._close_session(crashed=True)  # session précédente sans FIN = crash
            self.cur_date = datetime.strptime(m.group(1), '%Y-%m-%d').date()
            start = datetime.strptime(m.group(1) + ' ' + m.group(2), '%Y-%m-%d %H:%M:%S')
            self.last_dt = start
            self.cur_session = {'start': start, 'end': None, 'crashed': False,
                                'loops': [], 'counts': Counter(), 'scenario_starts': 0}
            self._last_event = None
            return

        m = RE_SESSION_END.match(line)
        if m:
            if self.cur_session:
                self.cur_session['end'] = datetime.strptime(
                    m.group(1) + ' ' + m.group(2), '%Y-%m-%d %H:%M:%S')
                self._close_session(crashed=False)
            return

        m = RE_LOOP.match(line)
        if m and self.cur_session:
            h, mi, s = (int(x) for x in m.group(2).split(':'))
            dt = self._make_dt(h, mi, s, 0)
            n = int(m.group(1))
            loops = self.cur_session['loops']
            entry = {'n': n, 'ts': dt, 'duration_s': None}
            ref = loops[-1]['ts'] if loops else self.cur_session.get('scenario_start_ts')
            if ref is not None:
                entry['duration_s'] = (dt - ref).total_seconds()
            loops.append(entry)
            # Le marqueur BOUCLE #n = retour à l'event de départ : le spectacle
            # n se termine, le n+1 commence.
            self._close_show(dt, partial=False)
            self._start_show(dt, n + 1)
            return

        m = RE_LINE.match(line)
        if not m:
            return  # stack trace ou ligne libre : comptée nulle part

        h, mi, s, ms = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        level = m.group(5).upper()
        msg = m.group(6)
        dt = self._make_dt(h, mi, s, ms)

        if self.cur_session is not None:
            self.cur_session['counts'][level] += 1
        self.level_per_hour[dt.strftime('%Y-%m-%d %H')][level] += 1
        if len(self.level_per_hour) > 5000:   # ~200 jours d'heures distinctes
            for old in sorted(self.level_per_hour)[:500]:
                del self.level_per_hour[old]

        if level in ('ERROR', 'EXCPT', 'ASSERT') or level == 'WARN':
            self.recent_errors.append(
                {'ts': dt.isoformat(), 'level': level, 'msg': msg[:400]})
            self._rec(dt, 'err', level, msg[:200])
            # Détection de nouveauté : signature = message aux nombres/ids près
            sig = re.sub(r'[0-9a-f]{16,}', '#', re.sub(r'\d+', 'N', msg))[:120]
            entry = self.error_signatures.get(sig)
            if entry is None and len(self.error_signatures) < 500:
                self.error_signatures[sig] = entry = {
                    'sig': sig, 'first_seen': dt.isoformat(), 'level': level,
                    'example': msg[:180], 'count': 0}
            if entry is not None:
                entry['count'] += 1
                if level != 'WARN':
                    entry['level'] = level

        self._parse_message(dt, msg)

    def _close_session(self, crashed):
        self._scenario_running = False
        if self.cur_session is None:
            return
        self._close_show(self.last_dt, partial=True)
        self.cur_session['crashed'] = crashed and self.cur_session['end'] is None
        if self.cur_session['end'] is None:
            self.cur_session['end'] = self.last_dt
        self.sessions.append(self.cur_session)
        self.cur_session = None

    def _parse_message(self, dt, msg):
        m = RE_FIRE_EVENT.match(msg)
        if m:
            ev_id = m.group(1)
            st = self.event_stats[ev_id]
            st['count'] += 1
            prev_id = None
            if self._last_event is not None:
                prev_dt, prev_id = self._last_event
                dur = (dt - prev_dt).total_seconds()
                if 0 <= dur < 3600:
                    self.event_stats[prev_id]['durations'].append(dur)
                    self._rec(dt, 'evdur', prev_id, round(dur, 2))
            self._last_event = (dt, ev_id)
            self.recent_events.append({'ts': dt.isoformat(), 'id': ev_id,
                                       'epoch': dt.timestamp()})
            # b = event précédent : permet de reconstituer les transitions
            # (et donc les choix faits aux embranchements / interactions).
            self._rec(dt, 'fire', ev_id, prev_id)
            return

        if RE_STOP_SCENAR.search(msg):
            self._scenario_running = False
            self._last_event = None
            self._close_show(dt, partial=True)   # arrêt en cours de spectacle
            return

        m = RE_START_SCENAR.search(msg)
        if m:
            self._scenario_running = True
            name = (m.group(1) or '').strip() or None
            if name:
                self._current_scenario = name
            if self.cur_session is not None:
                self.cur_session['scenario_starts'] += 1
                self.cur_session.setdefault('scenario_start_ts', dt)
                if name:
                    self.cur_session.setdefault('scenario', name)
            self._last_event = None
            self._close_show(dt, partial=True)   # restart au milieu d'un spectacle
            self._start_show(dt, 1)
            return

        m = RE_WS_OUT.match(msg)
        if m:
            e = self.esp[m.group(1)]
            e['ws_out'] += 1
            e['ip'] = m.group(2)
            e['last_out'] = dt.isoformat()
            am = RE_WS_OUT_ACT.search(m.group(4))
            action = am.group(1) if am else '?'
            e['actions'][action] += 1
            e['last_out_action'] = action
            self._rec(dt, 'ws_out', m.group(1), action)
            return

        m = RE_CONNECTED.search(msg)
        if m:
            e = self.esp[m.group(1)]
            e['connects'] += 1
            e['ip'] = m.group(2)
            ms_val = int(m.group(4))
            e['connect_ms'].append(ms_val)
            if len(e['connect_ms']) > 200:
                del e['connect_ms'][:100]
            e['connect_ms_last'] = ms_val
            self._rec(dt, 'connect', m.group(1), ms_val)
            return

        m = RE_CONN_FAILED.search(msg)
        if m:
            e = self.esp[m.group(1)]
            e['failures'] += 1
            e['ip'] = m.group(2)
            e['failure_last_cause'] = m.group(4)[:200]
            e['failure_last'] = dt.isoformat()
            self._rec(dt, 'fail', m.group(1), m.group(4)[:120])
            return

        m = RE_ACTION_LOST.search(msg)
        if m:
            e = self.esp[m.group(1)]
            e['lost'] += 1
            e['ip'] = m.group(2)
            e['lost_last'] = dt.isoformat()
            self._rec(dt, 'lost', m.group(1))
            return

        m = RE_SOUND_OK.search(msg)
        if m:
            self.sound_plays += 1
            self.sound_by_channel[m.group(2)] += 1
            self.sound_files[os.path.basename(m.group(1))] += 1
            self._rec(dt, 'sound', m.group(2), os.path.basename(m.group(1)))
            return

        if RE_SOUND_ERR.search(msg):
            self.sound_errors += 1
            self._rec(dt, 'sound_err')
            return

        m = RE_BUTTON.search(msg)
        if m:
            self.buttons[m.group(1)] += 1
            self._rec(dt, 'button', m.group(1))
            return

        if RE_WS_IN.search(msg):
            m = RE_WS_IN_SUPPR.search(msg)
            if m:
                self.ws_in_suppressed += int(m.group(1))
                self._rec(dt, 'ws_in', None, int(m.group(1)))
            else:
                self.ws_in_shown += 1
                nm = RE_WS_IN_NAMED.search(msg)   # nom de l'ESP émetteur (format récent)
                name = nm.group(1) if nm else None
                if name:
                    self.esp[name]['ws_in'] += 1
                    self.esp[name]['last_in'] = dt.isoformat()
                self._rec(dt, 'ws_in', name, 1)

    # ─── Lecture du fichier ──────────────────────────────────────────────────
    def _list_archives(self):
        """Archives de rotation, triées : [(epoch_fin_du_contenu, nom_fichier)].
        La date du nom = moment de la rotation = FIN du contenu de l'archive."""
        log_dir = os.path.dirname(self.log_path) or '.'
        stem, ext = os.path.splitext(os.path.basename(self.log_path))
        date_re = re.compile(r'_(\d{4}-\d{2}-\d{2})(?:_(\d{2})(\d{2}))?' + re.escape(ext) + '$')
        out = []
        try:
            for fn in os.listdir(log_dir):
                if not (fn.startswith(stem + '_') and fn.endswith(ext)):
                    continue
                m = date_re.search(fn)
                if m:
                    end_dt = datetime.strptime(
                        f"{m.group(1)} {m.group(2) or '23'}{m.group(3) or '59'}",
                        '%Y-%m-%d %H%M')
                    end_ts = end_dt.timestamp()
                else:
                    try:
                        end_ts = os.path.getmtime(os.path.join(log_dir, fn))
                    except OSError:
                        continue
                out.append((end_ts, fn))
        except OSError:
            pass
        return sorted(out)

    def parse_full(self, archive_names=None):
        """(Re)parse : les archives demandées (en ordre chronologique) puis le
        fichier vivant. Sans argument : fichier vivant seul (démarrage rapide,
        les archives se chargent ensuite selon la période demandée)."""
        with self.lock:
            self.reset()
            self._pos = 0
            self._buffer = ''
            log_dir = os.path.dirname(self.log_path) or '.'
            selected = []
            if archive_names:
                selected = [fn for _end, fn in self._list_archives()
                            if fn in archive_names]
            for fn in selected:
                try:
                    with io.open(os.path.join(log_dir, fn), 'r',
                                 encoding='utf-8', errors='replace') as f:
                        for line in f:
                            try:
                                self.feed_line(line)
                            except Exception:
                                pass
                except OSError:
                    pass
            self._archives_loaded = set(selected)
            self._read_new()

    def ensure_coverage(self, t0):
        """Garantit que les archives couvrant [t0, maintenant] sont chargées.
        Appelé par le serveur avec le début de la période demandée par
        l'utilisateur : c'est la période qui pilote ce qu'on lit, rien d'autre."""
        needed = {fn for end_ts, fn in self._list_archives() if end_ts >= t0}
        with self.lock:
            missing = needed - self._archives_loaded
        if missing:
            self.parse_full(archive_names=needed | self._archives_loaded)

    def poll(self):
        """Lit les octets ajoutés depuis le dernier appel (tail incrémental)."""
        with self.lock:
            try:
                size = os.path.getsize(self.log_path)
            except OSError:
                return
            if size < self._pos:
                # Rotation : le fichier vivant a été archivé et recréé — on
                # continue depuis 0 SANS perdre l'historique en mémoire
                # (l'ancien contenu est déjà parsé, l'archive le conserve).
                self._pos = 0
                self._buffer = ''
            self._read_new()

    def _read_new(self):
        try:
            with io.open(self.log_path, 'r', encoding='utf-8', errors='replace') as f:
                f.seek(self._pos)
                chunk = f.read()
                self._pos = f.tell()
        except OSError:
            return
        if not chunk:
            return
        data = self._buffer + chunk
        lines = data.split('\n')
        self._buffer = lines.pop()    # dernière ligne possiblement incomplète
        for line in lines:
            try:
                self.feed_line(line)
            except Exception:
                pass                  # une ligne inattendue ne doit jamais tuer le tail

    # ─── Résumé pour l'API ───────────────────────────────────────────────────
    def summary(self):
        with self.lock:
            return self._summary_unlocked()

    def _summary_unlocked(self):
        now = time.time()
        all_sessions = self.sessions + ([self.cur_session] if self.cur_session else [])

        # Boucles (toutes sessions confondues, dans l'ordre)
        loop_durs, recent_loops = [], []
        for si, sess in enumerate(all_sessions):
            for lp in sess['loops']:
                if lp['duration_s'] is not None:
                    loop_durs.append(lp['duration_s'])
                recent_loops.append({'session': si, 'n': lp['n'],
                                     'ts': lp['ts'].isoformat(),
                                     'duration_s': lp['duration_s']})
        recent_loops = recent_loops[-MAX_RECENT_LOOPS:]
        med = statistics.median(loop_durs) if loop_durs else None
        drift_pct = None
        if med and len(loop_durs) >= 6:
            recent_avg = sum(loop_durs[-3:]) / 3
            drift_pct = round((recent_avg - med) / med * 100, 1)

        sessions_out = []
        for sess in all_sessions:
            is_current = sess is self.cur_session
            end = sess['end']
            sessions_out.append({
                'start': sess['start'].isoformat(),
                'end': end.isoformat() if end else None,
                'open': is_current,
                'crashed': sess.get('crashed', False),
                'loops': len(sess['loops']),
                'scenario': sess.get('scenario'),
                'scenario_starts': sess.get('scenario_starts', 0),
                'counts': dict(sess['counts']),
                'duration_s': ((end or self.last_dt or sess['start'])
                               - sess['start']).total_seconds(),
            })

        # Event en cours (pour l'alerte "event bloqué")
        current_event = None
        if self.recent_events:
            last_ev = self.recent_events[-1]
            st = self.event_stats.get(last_ev['id'])
            durs = st['durations'] if st else []
            current_event = {
                'id': last_ev['id'],
                'ts': last_ev['ts'],
                'age_s': round(now - last_ev.get('epoch', now), 1),
                'hist_max_s': round(max(durs), 1) if durs else None,
            }

        esp_out = {}
        for name, e in self.esp.items():
            cm = e['connect_ms']
            esp_out[name] = {
                'ip': e['ip'], 'ws_out': e['ws_out'],
                'ws_in': e['ws_in'], 'last_in': e['last_in'],
                'actions': dict(e['actions'].most_common(10)),
                'last_out': e['last_out'], 'last_out_action': e['last_out_action'],
                'lost': e['lost'], 'lost_last': e['lost_last'],
                'connects': e['connects'],
                'connect_ms_avg': round(sum(cm) / len(cm), 1) if cm else None,
                'connect_ms_last': e['connect_ms_last'],
                'failures': e['failures'],
                'failure_last_cause': e['failure_last_cause'],
                'failure_last': e['failure_last'],
            }

        events_top = []
        for ev_id, st in sorted(self.event_stats.items(),
                                key=lambda kv: -kv[1]['count'])[:200]:
            durs = st['durations']
            events_top.append({
                'id': ev_id, 'count': st['count'],
                'avg_s': round(sum(durs) / len(durs), 2) if durs else None,
                'min_s': round(min(durs), 2) if durs else None,
                'max_s': round(max(durs), 2) if durs else None,
                'stdev_s': round(statistics.stdev(durs), 2) if len(durs) > 1 else None,
            })

        return {
            'generated_at': now,
            'log_path': self.log_path,
            'log_size': os.path.getsize(self.log_path) if os.path.exists(self.log_path) else 0,
            'total_lines': self.total_lines,
            'last_line_age_s': round(now - self.last_line_wall, 1) if self.last_line_wall else None,
            'last_ts': self.last_dt.isoformat() if self.last_dt else None,
            'session_open': self.cur_session is not None,
            'scenario_running': self.cur_session is not None and self._scenario_running,
            'sessions': sessions_out,
            'loops': {
                'count': len(loop_durs),
                'median_s': round(med, 1) if med else None,
                'min_s': round(min(loop_durs), 1) if loop_durs else None,
                'max_s': round(max(loop_durs), 1) if loop_durs else None,
                'last_s': round(loop_durs[-1], 1) if loop_durs else None,
                'drift_pct': drift_pct,
                'recent': recent_loops,
            },
            'events': {'top': events_top,
                       'recent': list(self.recent_events)[-40:],
                       'last': self.recent_events[-1] if self.recent_events else None},
            'current_event': current_event,
            'scenario_current': self._current_scenario,
            'error_signatures': sorted(self.error_signatures.values(),
                                       key=lambda e: e['first_seen'])[-200:],
            'esp': esp_out,
            'sound': {'plays': self.sound_plays,
                      'by_channel': dict(self.sound_by_channel),
                      'top_files': self.sound_files.most_common(15),
                      'errors': self.sound_errors},
            'buttons': dict(self.buttons),
            'ws_in': {'shown': self.ws_in_shown, 'suppressed': self.ws_in_suppressed},
            'level_per_hour': {h: dict(c) for h, c in
                               sorted(self.level_per_hour.items())[-48:]},
            'errors_recent': list(self.recent_errors)[-80:],
        }


    # ─── Requêtes par spectacle / par plage de dates ─────────────────────────
    def shows_summary(self):
        """Spectacles avec compteurs rapides (scan fusionné des records)."""
        with self.lock:
            shows = [dict(s) for s in self.shows]
            if self._open_show is not None:
                cur = dict(self._open_show)
                cur['open'] = True
                now_ts = self.last_dt.timestamp() if self.last_dt else cur['start']
                cur['duration_s'] = round(now_ts - cur['start'], 1)
                shows.append(cur)
            records = self.records

            for s in shows:
                s.setdefault('open', False)
                s.update(errors=0, warns=0, lost=0, sounds=0, ws_out=0)

            ri, n = 0, len(records)
            for s in shows:
                end = s['end'] if s['end'] is not None else float('inf')
                while ri < n and records[ri][0] < s['start']:
                    ri += 1
                while ri < n and records[ri][0] <= end:
                    _, kind, a, _b = records[ri]
                    if kind == 'err':
                        if a == 'WARN':
                            s['warns'] += 1
                        else:
                            s['errors'] += 1
                    elif kind == 'lost':
                        s['lost'] += 1
                    elif kind == 'sound':
                        s['sounds'] += 1
                    elif kind == 'ws_out':
                        s['ws_out'] += 1
                    ri += 1

            for i, s in enumerate(shows):
                s['id'] = i
                s['start_iso'] = datetime.fromtimestamp(s['start']).isoformat()
                s['end_iso'] = (datetime.fromtimestamp(s['end']).isoformat()
                                if s['end'] else None)

            days = sorted({s['day'] for s in shows} |
                          {sess['start'].strftime('%Y-%m-%d')
                           for sess in self.sessions}
                          | ({self.cur_session['start'].strftime('%Y-%m-%d')}
                             if self.cur_session else set()))
            return {'shows': shows, 'days': days}

    def stats_range(self, ranges):
        """Agrégats sur une liste de plages [(t0, t1), ...] (epoch, triées)."""
        with self.lock:
            ranges = sorted((float(a), float(b)) for a, b in ranges if b > a)
            recs = self.records

            esp = defaultdict(lambda: {'ws_out': 0, 'actions': Counter(), 'lost': 0,
                                       'connects': 0, 'connect_ms': [], 'failures': 0,
                                       'last_out': None, 'last_out_action': None,
                                       'failure_last_cause': None,
                                       'down_since': None, 'downtime': 0.0, 'outages': 0,
                                       'ws_in': 0, 'last_in': None})
            ev = defaultdict(lambda: {'count': 0, 'durations': []})
            ev_hour = defaultdict(lambda: defaultdict(lambda: [0.0, 0]))  # {ev: {heure: [somme, n]}}
            # Files séparées : les vraies erreurs sont rares et ne doivent jamais
            # être évincées de la liste par le volume des warnings.
            warn_recent = deque(maxlen=100)
            hard_recent = deque(maxlen=100)
            err_groups = {}   # msg normalisé -> {count, level, example, last_ts}
            counts = Counter()
            per_hour = defaultdict(Counter)
            act_hour = defaultdict(Counter)   # activité par heure (graphes)
            # Par ESP et par heure : envoyés / reçus / échecs conn. / perdus.
            esp_hour = defaultdict(lambda: {'sent': defaultdict(int), 'recv': defaultdict(int),
                                            'fail': defaultdict(int), 'lost': defaultdict(int)})
            sound_ch = Counter()
            sound_files = Counter()
            sound_plays = sound_errors = 0
            ws_in_total = 0
            buttons = Counter()
            actions_total = Counter()
            transitions = defaultdict(Counter)   # prev_event -> {next_event: n}
            trans_durs = defaultdict(list)       # (prev, next) -> [durées de prev]
            pending_evdur = None                 # (event_id, dur) en attente du fire suivant

            ri, n = 0, len(recs)
            for t0, t1 in ranges:
                lo, hi = ri, n
                while lo < hi:                      # bisect vers t0
                    mid = (lo + hi) // 2
                    if recs[mid][0] < t0:
                        lo = mid + 1
                    else:
                        hi = mid
                ri = lo
                while ri < n and recs[ri][0] <= t1:
                    ts, kind, a, b = recs[ri]
                    ri += 1
                    if kind == 'ws_out':
                        e = esp[a]
                        e['ws_out'] += 1
                        e['actions'][b] += 1
                        e['last_out'] = ts
                        e['last_out_action'] = b
                        actions_total[b] += 1
                        hstr = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H')
                        act_hour[hstr]['ws_out'] += 1
                        esp_hour[a]['sent'][hstr] += 1
                    elif kind == 'err':
                        counts[a] += 1
                        hour = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H')
                        per_hour[hour][a] += 1
                        entry = {'ts': datetime.fromtimestamp(ts).isoformat(),
                                 'level': a, 'msg': b}
                        (warn_recent if a == 'WARN' else hard_recent).append(entry)
                        # Groupage : mêmes messages aux nombres/ids près
                        key = re.sub(r'[0-9a-f]{16,}', '#', re.sub(r'\d+', 'N', b or ''))[:120]
                        g = err_groups.get(key)
                        if g is None:
                            err_groups[key] = g = {'count': 0, 'level': a,
                                                   'example': (b or '')[:180],
                                                   'last_ts': None}
                        g['count'] += 1
                        if a != 'WARN':
                            g['level'] = a   # une vraie erreur prime sur le warning
                        g['last_ts'] = datetime.fromtimestamp(ts).isoformat()
                    elif kind == 'evdur':
                        ev[a]['durations'].append(b)
                        cell = ev_hour[a][datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H')]
                        cell[0] += b
                        cell[1] += 1
                        pending_evdur = (a, b)
                    elif kind == 'fire':
                        ev[a]['count'] += 1
                        if b:
                            transitions[b][a] += 1
                            # la durée de l'event précédent, rattachée à la
                            # transition -> permet de classer choix vs timeout
                            if pending_evdur and pending_evdur[0] == b:
                                lst = trans_durs[(b, a)]
                                if len(lst) < 500:
                                    lst.append(pending_evdur[1])
                        pending_evdur = None
                    elif kind == 'connect':
                        e = esp[a]
                        e['connects'] += 1
                        e['connect_ms'].append(b)
                        if e['down_since'] is not None:   # fin de panne
                            e['downtime'] += ts - e['down_since']
                            e['down_since'] = None
                    elif kind == 'fail':
                        e = esp[a]
                        e['failures'] += 1
                        e['failure_last_cause'] = b
                        esp_hour[a]['fail'][datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H')] += 1
                        if e['down_since'] is None:       # début de panne
                            e['down_since'] = ts
                            e['outages'] += 1
                    elif kind == 'lost':
                        esp[a]['lost'] += 1
                        esp_hour[a]['lost'][datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H')] += 1
                    elif kind == 'sound':
                        sound_plays += 1
                        sound_ch[a] += 1
                        sound_files[b] += 1
                        act_hour[datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H')]['sound'] += 1
                    elif kind == 'sound_err':
                        sound_errors += 1
                    elif kind == 'ws_in':
                        ws_in_total += b or 1
                        hstr = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H')
                        act_hour[hstr]['ws_in'] += b or 1
                        if a:   # nom de l'ESP émetteur (format récent du log)
                            esp[a]['ws_in'] += b or 1
                            esp[a]['last_in'] = ts
                            esp_hour[a]['recv'][hstr] += b or 1
                    elif kind == 'button':
                        buttons[a] += 1

            # Spectacles complets dont le début tombe dans les plages
            loops_in, recent, scen_names = [], [], set()
            for s in self.shows:
                if any(t0 <= s['start'] < t1 for t0, t1 in ranges) and s.get('scenario'):
                    scen_names.add(s['scenario'])
                if s['duration_s'] is None or s.get('partial'):
                    continue
                # borne haute stricte : la fin du spectacle n == le début du n+1
                if any(t0 <= s['start'] < t1 for t0, t1 in ranges):
                    loops_in.append(s['duration_s'])
                    recent.append({'n': s['n'], 'ts':
                                   datetime.fromtimestamp(s['start']).isoformat(),
                                   'duration_s': s['duration_s'],
                                   'scenario': s.get('scenario') or ''})
            med = statistics.median(loops_in) if loops_in else None

            # Fin de plage : une panne encore ouverte compte jusqu'à la borne haute
            range_end = min(ranges[-1][1], time.time()) if ranges else time.time()

            esp_out = {}
            for name, e in esp.items():
                cm = e['connect_ms']
                downtime = e['downtime']
                if e['down_since'] is not None:
                    downtime += max(0.0, range_end - e['down_since'])
                # Tendance du temps de connexion : dernières vs premières
                trend = None
                if len(cm) >= 6:
                    trend = round(sum(cm[-3:]) / 3 - sum(cm[:3]) / 3, 1)
                esp_out[name] = {
                    'ws_out': e['ws_out'],
                    'actions': dict(Counter(e['actions']).most_common(8)),
                    'lost': e['lost'], 'connects': e['connects'],
                    'connect_ms_avg': round(sum(cm) / len(cm), 1) if cm else None,
                    'connect_ms_trend': trend,
                    'failures': e['failures'],
                    'failure_last_cause': e['failure_last_cause'],
                    'downtime_s': round(downtime, 1),
                    'outages': e['outages'],
                    'ws_in': e['ws_in'],
                    'last_in': (datetime.fromtimestamp(e['last_in']).isoformat()
                                if e['last_in'] else None),
                    'last_out': (datetime.fromtimestamp(e['last_out']).isoformat()
                                 if e['last_out'] else None),
                    'last_out_action': e['last_out_action'],
                }

            # Embranchements : events avec plusieurs successeurs observés = les
            # choix faits (interactions visiteurs, conditions capteurs).
            branches = []
            for prev_id, nexts in transitions.items():
                if len(nexts) < 2:
                    continue
                total = sum(nexts.values())
                branches.append({
                    'id': prev_id, 'total': total,
                    'options': [{'id': nid, 'count': n}
                                for nid, n in nexts.most_common()],
                })
            branches.sort(key=lambda br: -br['total'])
            branches = branches[:20]

            # Axe temporel commun aux courbes de durée d'events (heures observées).
            _ev_hours = sorted({h for hh in ev_hour.values() for h in hh})
            _ehidx = {h: i for i, h in enumerate(_ev_hours)}

            events_top = []
            for ev_id, st in sorted(ev.items(), key=lambda kv: -kv[1]['count'])[:200]:
                durs = st['durations']
                # Durée moyenne par heure (None si l'event ne s'est pas joué cette heure).
                dur_series = [None] * len(_ev_hours)
                for h, (s, nn) in ev_hour.get(ev_id, {}).items():
                    dur_series[_ehidx[h]] = round(s / nn, 1) if nn else None
                events_top.append({
                    'id': ev_id, 'count': st['count'],
                    'avg_s': round(sum(durs) / len(durs), 2) if durs else None,
                    'min_s': round(min(durs), 2) if durs else None,
                    'max_s': round(max(durs), 2) if durs else None,
                    'stdev_s': round(statistics.stdev(durs), 2) if len(durs) > 1 else None,
                    'dur_hourly': dur_series,
                })

            # Séries par ESP (envoyés/reçus/échecs/perdus) alignées sur une liste
            # d'heures commune à tous les ESP actifs de la période.
            _all_hours = set()
            for d in esp_hour.values():
                for kk in ('sent', 'recv', 'fail', 'lost'):
                    _all_hours.update(d[kk])
            _rx_hours = sorted(_all_hours)
            _hidx = {h: i for i, h in enumerate(_rx_hours)}
            _rx_series = {}
            for name, d in esp_hour.items():
                s = {}
                for kk in ('sent', 'recv', 'fail', 'lost'):
                    arr = [0] * len(_rx_hours)
                    for h, nn in d[kk].items():
                        arr[_hidx[h]] = nn
                    s[kk] = arr
                _rx_series[name] = s

            return {
                'ranges': [[datetime.fromtimestamp(a).isoformat(),
                            datetime.fromtimestamp(b).isoformat()] for a, b in ranges],
                'loops': {
                    'count': len(loops_in),
                    'median_s': round(med, 1) if med else None,
                    'min_s': round(min(loops_in), 1) if loops_in else None,
                    'max_s': round(max(loops_in), 1) if loops_in else None,
                    'recent': recent[-MAX_RANGE_LOOPS:],
                },
                'esp': esp_out,
                'scenario_names': sorted(scen_names),
                'events': {'top': events_top, 'fired_ids': list(ev.keys()),
                           'branches': branches, 'hours': _ev_hours},
                'transitions': {prev: {nxt: {'count': n,
                                             'durs': [round(x, 1) for x in
                                                      trans_durs.get((prev, nxt), [])]}
                                       for nxt, n in nexts.items()}
                                for prev, nexts in transitions.items()},
                'errors_recent': sorted(list(warn_recent) + list(hard_recent),
                                        key=lambda e: e['ts']),
                'hard_errors_recent': list(hard_recent),
                # Les vraies erreurs passent toujours devant : rares mais précieuses,
                # elles ne doivent jamais être évincées par le volume des warnings.
                'errors_top': ([g for g in sorted(err_groups.values(),
                                                  key=lambda g: -g['count'])
                                if g['level'] != 'WARN']
                               + [g for g in sorted(err_groups.values(),
                                                    key=lambda g: -g['count'])
                                  if g['level'] == 'WARN'])[:16],
                'counts': dict(counts),
                'level_per_hour': {h: dict(c) for h, c in sorted(per_hour.items())},
                'activity_per_hour': {h: dict(c) for h, c in sorted(act_hour.items())},
                'sound': {'plays': sound_plays, 'by_channel': dict(sound_ch),
                          'top_files': sound_files.most_common(10),
                          'errors': sound_errors},
                'ws_in': {'count': ws_in_total},
                'buttons': dict(buttons),
                'actions_total': dict(actions_total.most_common(15)),
                # Horodatage du plus vieux enregistrement encore en mémoire :
                # une période qui commence avant = incidents/activité purgés.
                'records_from': (datetime.fromtimestamp(recs[0][0]).isoformat()
                                 if recs else None),
                # Réception par ESP et par heure : mini-courbes pour repérer quel
                # capteur décroche et quand. Aligné sur la liste d'heures commune.
                'esp_rx_hours': _rx_hours,
                'esp_rx_series': _rx_series,
            }


if __name__ == '__main__':
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..', 'Logs', 'scenario_console.txt')
    analyzer = LogAnalyzer(os.path.normpath(path))
    t0 = time.time()
    analyzer.parse_full()
    out = analyzer.summary()
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"\n-- parse de {out['total_lines']} lignes en {time.time() - t0:.2f}s --",
          file=sys.stderr)
