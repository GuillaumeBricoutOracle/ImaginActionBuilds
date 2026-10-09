#!/usr/bin/env python3
"""Analyseur des journaux scénario (Logs/scenario_console_<AAAA-MM-JJ>.txt).

Unity (ScenarioFileLogger) écrit UN FICHIER PAR JOUR. L'analyseur lit tous les
fichiers journaliers d'un dossier, dans l'ordre chronologique, et maintient des
statistiques : sessions, boucles, events, fiabilité ESP, audio, erreurs.

Suivi incrémental : `rebuild()` relit la fenêtre de fichiers au démarrage, puis
`poll()` (toutes les secondes) ne lit que les octets ajoutés au fichier le plus
récent (le « vivant »). Dès qu'un fichier est ajouté, retiré ou remplacé dans le
dossier, tout est relu : ce qu'il y a dans Logs/ est exactement ce que le
dashboard montre.

Testable en standalone :  python loganalyzer.py [dossier_Logs]
Découper un ancien journal unique en fichiers par jour :
                          python loganalyzer.py --split ancien.txt [--out Logs/] [--force]
"""

import io
import json
import os
import re
import statistics
import sys
import threading
import time
from collections import Counter, defaultdict, deque, namedtuple
from datetime import datetime, timedelta

# ─── Regex des lignes connues ────────────────────────────────────────────────
RE_SESSION      = re.compile(r'^=+ SESSION (\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) =+')
RE_SESSION_END  = re.compile(r'^=+ FIN SESSION (\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) \((\d+) boucle')
# Passage de minuit : le fichier du lendemain reprend la session en cours.
RE_SESSION_CONT = re.compile(r'^=+ SUITE SESSION (\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) =+')
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
# Pas de « Action perdue » exigé en fin de ligne : la cause (message d'exception
# Windows) contient un retour à la ligne, qui repousse la fin du message sur la
# ligne suivante. Le préfixe suffit à identifier la ligne.
RE_ACTION_LOST  = re.compile(r'Socket non ouverte pour (\S+) \(([\d.]+):(\d+)\)')
# Ampoules Yeelight (TCP direct, pas de WebSocket) : mêmes notions, autre format.
RE_YL_CONNECTED = re.compile(r'\[Yeelight\] Connecté : (\S+) ([\d.]+):(\d+)')
RE_YL_FAILED    = re.compile(r'\[Yeelight\] Connexion échouée (\S+?): (.+?)(?: [—-]+ |$)')
RE_YL_OUT       = re.compile(r"^\[YEELIGHT OUT\] (\S+) \(([\d.]+):(\d+)\) => action='([^']*)'")
# Le scénario vise un nom d'appareil absent de la liste du matériel (faute de
# frappe, renommage) : Unity jette l'action. Invisible sans ça.
RE_HW_MISSING   = re.compile(r"Hardware introuvable : '([^']+)'")
RE_SOUND_OK     = re.compile(r"\[Son\]\s+(?:[^\x00-\x7F]+\s+)?Lecture OK : '(.+?)' sur channel '(.+?)'")
RE_SOUND_ERR    = re.compile(r'\[Son\] (Fichier audio introuvable|Erreur chargement audio|Clip null)')
RE_BUTTON       = re.compile(r'\[ControllerManager\]\[IN\]\s+(?:[^\x00-\x7F]+\s+)?button=(\S+)')
RE_WS_IN        = re.compile(r'\[WS IN\]')
RE_WS_IN_NAMED  = re.compile(r'\[WS IN\](?:</color>)?\s+(\S+) <= ')
RE_WS_IN_SUPPR  = re.compile(r'(\d+) message\(s\) non affiché')

# Fichiers journaliers écrits par Unity : scenario_console_<AAAA-MM-JJ>.txt
FILE_PREFIX = 'scenario_console_'
RE_DAY_FILE = re.compile(r'^scenario_console_(\d{4}-\d{2}-\d{2})\.txt$')
LogFile = namedtuple('LogFile', 'date name size mtime ino')
# Fichiers-jours chargés d'office (les plus récents). Les plus anciens sont
# chargés quand on consulte leur période (ensure_coverage) : inutile de
# parser un mois de nuits pour afficher la journée d'hier.
DEFAULT_MAX_DAYS = 14

MAX_RECENT_ERRORS = 300
MAX_RECENT_EVENTS = 200
MAX_RECENT_LOOPS  = 100     # pour le résumé live (vue d'ensemble)
MAX_RANGE_LOOPS   = 5000    # pour l'analyse d'une période : on veut tout voir
MAX_RECORDS       = 800_000   # ~1 semaine de nuits chargées (~450 Mo au plafond) ;
                              # au-delà les plus vieux sont purgés -> les stats
                              # d'incidents/activité d'une période plus ancienne
                              # ne sont plus disponibles (le dashboard le signale).


class LogAnalyzer:
    def __init__(self, log_dir, max_days=None):
        # Ancien usage (chemin d'un fichier unique) toléré : on prend son dossier.
        if str(log_dir).lower().endswith('.txt'):
            log_dir = os.path.dirname(log_dir)
        self.log_dir = os.path.normpath(log_dir or '.')
        self.max_days = DEFAULT_MAX_DAYS if max_days is None else max_days
        self.lock = threading.Lock()           # protège l'état servi à l'API
        self._rebuild_lock = threading.Lock()  # une seule relecture à la fois
        self._from_date = None     # 'AAAA-MM-JJ' : fenêtre étendue par ensure_coverage
        self._cutoff = None        # date du plus ancien fichier de la fenêtre courante
        self._loaded = []          # noms des fichiers chargés, ordre chronologique
        self._live = None          # fichier vivant (le plus récent), suivi en tail
        self._live_ino = None      # identité disque du vivant (détecte un remplacement)
        self._pos = 0              # octets déjà lus du fichier vivant
        self._buffer = b''         # fin de ligne incomplète du fichier vivant
        self._snapshot = {}        # {nom: (taille, mtime, ino)} des fichiers chargés hors vivant
        self._pending = None       # changement détecté mais pas encore stable (copie en cours)
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
        # first_event : premier event tiré pendant ce spectacle. Le serveur s'en
        # sert pour retrouver le scénario RÉELLEMENT joué (le nom loggué au
        # START SCENARIO est celui du début de playlist, pas forcément le bon).
        self._open_show = {'n': n, 'start': dt.timestamp(), 'end': None,
                           'duration_s': None, 'day': dt.strftime('%Y-%m-%d'),
                           'partial': False, 'scenario': self._current_scenario,
                           'first_event': None}

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
            self._open_session(start)
            return

        m = RE_SESSION_CONT.match(line)
        if m:
            # Minuit : Unity a basculé sur le fichier du lendemain, la session
            # continue. Si le fichier de la veille a été retiré du dossier, la
            # session n'a pas d'en-tête : on l'ouvre ici (implicite).
            self.cur_date = datetime.strptime(m.group(1), '%Y-%m-%d').date()
            dt = datetime.strptime(m.group(1) + ' ' + m.group(2), '%Y-%m-%d %H:%M:%S')
            self.last_dt = dt
            if self.cur_session is None:
                self._open_session(dt, implicit=True)
            return

        m = RE_SESSION_END.match(line)
        if m:
            if self.cur_session:
                self.cur_session['end'] = datetime.strptime(
                    m.group(1) + ' ' + m.group(2), '%Y-%m-%d %H:%M:%S')
                self._close_session(crashed=False)
            return

        m = RE_LOOP.match(line)
        if m:
            h, mi, s = (int(x) for x in m.group(2).split(':'))
            dt = self._make_dt(h, mi, s, 0)
            if self.cur_session is None:
                self._open_session(dt, implicit=True)
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

        if self.cur_session is None:
            # Lignes sans en-tête SESSION (fichier de la veille retiré, en-tête
            # perdu) : session implicite, pour que les stats par session et le
            # temps hors ligne des appareils restent calculables.
            self._open_session(dt, implicit=True)
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

    def _open_session(self, start, implicit=False):
        self.cur_session = {'start': start, 'end': None, 'crashed': False,
                            'loops': [], 'counts': Counter(), 'scenario_starts': 0,
                            'implicit': implicit}
        self._last_event = None

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
            if self._open_show is not None and not self._open_show.get('first_event'):
                self._open_show['first_event'] = ev_id
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

        m = RE_YL_OUT.search(msg)
        if m:
            e = self.esp[m.group(1)]
            e['ws_out'] += 1
            e['ip'] = m.group(2)
            e['last_out'] = dt.isoformat()
            e['actions'][m.group(4)] += 1
            e['last_out_action'] = m.group(4)
            self._rec(dt, 'ws_out', m.group(1), m.group(4))
            return

        m = RE_YL_CONNECTED.search(msg)
        if m:
            e = self.esp[m.group(1)]
            e['connects'] += 1
            e['ip'] = m.group(2)
            self._rec(dt, 'connect', m.group(1), 0)   # pas de durée mesurée côté Yeelight
            return

        m = RE_YL_FAILED.search(msg)
        if m:
            e = self.esp[m.group(1)]
            e['failures'] += 1
            e['failure_last_cause'] = m.group(2)[:200]
            e['failure_last'] = dt.isoformat()
            self._rec(dt, 'fail', m.group(1), m.group(2)[:120])
            return

        m = RE_ACTION_LOST.search(msg)
        if m:
            e = self.esp[m.group(1)]
            e['lost'] += 1
            e['ip'] = m.group(2)
            e['lost_last'] = dt.isoformat()
            self._rec(dt, 'lost', m.group(1))
            return

        m = RE_HW_MISSING.search(msg)
        if m:
            self._rec(dt, 'missing_hw', m.group(1))
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

    # ─── Lecture des fichiers (un par jour) ──────────────────────────────────
    def list_files(self):
        """Fichiers journaliers du dossier, triés par date : [LogFile(...)]."""
        out = []
        try:
            names = os.listdir(self.log_dir)
        except OSError:
            return out
        for fn in names:
            m = RE_DAY_FILE.match(fn)
            if not m:
                continue
            try:
                st = os.stat(os.path.join(self.log_dir, fn))
            except OSError:
                continue
            out.append(LogFile(m.group(1), fn, st.st_size, st.st_mtime, st.st_ino))
        out.sort()
        return out

    def files_info(self):
        """Pour l'API : chaque fichier-jour du dossier, chargé ou non."""
        loaded = set(self._loaded)
        return [{'date': f.date, 'name': f.name, 'size': f.size,
                 'loaded': f.name in loaded, 'live': f.name == self._live}
                for f in self.list_files()]

    def _window(self, files):
        """Fichiers à charger : les `max_days` plus récents, étendus jusqu'à
        `_from_date` si une période plus ancienne a été demandée."""
        if not files:
            return []
        cutoff = files[0].date
        if self.max_days and len(files) > self.max_days:
            cutoff = files[-self.max_days].date
        if self._from_date and self._from_date < cutoff:
            cutoff = self._from_date
        return [f for f in files if f.date >= cutoff]

    def _feed_bytes(self, data):
        """Découpe en lignes et parse ; retourne le reste (ligne incomplète)."""
        lines = data.split(b'\n')
        rest = lines.pop()
        for raw in lines:
            try:
                self.feed_line(raw.decode('utf-8', 'replace').lstrip('﻿'))
            except Exception:
                pass                  # une ligne inattendue ne doit jamais tuer le tail
        return rest

    def _read_new(self):
        """Lit les octets ajoutés au fichier vivant depuis le dernier appel."""
        if not self._live:
            return
        try:
            with open(os.path.join(self.log_dir, self._live), 'rb') as f:
                f.seek(self._pos)
                chunk = f.read()
                self._pos = f.tell()
        except OSError:
            return
        if chunk:
            self._buffer = self._feed_bytes(self._buffer + chunk)

    def _flush_buffer(self):
        """Dernière ligne sans retour chariot (fichier terminé) : on la parse."""
        if self._buffer:
            self._buffer = self._feed_bytes(self._buffer + b'\n')

    def _start_file(self, f):
        """Passe la lecture sur le fichier `f` (LogFile), qui devient le vivant."""
        self._live, self._live_ino, self._pos, self._buffer = f.name, f.ino, 0, b''
        self._loaded.append(f.name)
        # La date du nom fait foi pour les lignes sans en-tête SESSION.
        self.cur_date = datetime.strptime(f.date, '%Y-%m-%d').date()

    def rebuild(self):
        """Relit toute la fenêtre de fichiers dans un état NEUF, puis le
        substitue à l'état courant. L'API continue de répondre (ancien état)
        pendant la relecture : seul l'échange final prend le verrou."""
        with self._rebuild_lock:
            selected = self._window(self.list_files())
            fresh = LogAnalyzer(self.log_dir, self.max_days)
            fresh._from_date = self._from_date
            fresh._cutoff = selected[0].date if selected else None
            for f in selected:
                fresh._start_file(f)
                fresh._read_new()
                if f is not selected[-1]:
                    fresh._flush_buffer()
            fresh._snapshot = {f.name: (f.size, f.mtime, f.ino) for f in selected
                               if f.name != fresh._live}
            # Des fichiers déposés à la main peuvent être dans le désordre :
            # les requêtes par période supposent des enregistrements triés.
            fresh.records.sort(key=lambda r: r[0])
            fresh.shows.sort(key=lambda sh: sh['start'])
            fresh.sessions.sort(key=lambda se: se['start'])
            state = {k: v for k, v in fresh.__dict__.items()
                     if k not in ('lock', '_rebuild_lock')}
            with self.lock:
                self.__dict__.update(state)

    parse_full = rebuild    # ancien nom, gardé pour compatibilité

    def ensure_coverage(self, t0):
        """Charge aussi les fichiers plus anciens que la fenêtre si la période
        demandée (début t0, epoch) commence avant. Appelé par le serveur :
        c'est la période consultée qui pilote ce qu'on lit."""
        try:
            d = datetime.fromtimestamp(float(t0)).strftime('%Y-%m-%d')
        except (OverflowError, OSError, ValueError):
            return
        cutoff = self._cutoff
        if cutoff is None or d >= cutoff:
            return
        if not any(d <= f.date < cutoff for f in self.list_files()):
            return                      # rien de plus ancien sur le disque
        if self._from_date is None or d < self._from_date:
            self._from_date = d
        self.rebuild()

    def poll(self):
        """Toutes les secondes : tail du fichier vivant, ou relecture complète
        si le dossier a changé (fichier ajouté, retiré, remplacé)."""
        files = self.list_files()
        if self._cutoff is None:
            selected = self._window(files)
        else:
            # Fenêtre figée entre deux relectures : un nouveau jour s'ajoute
            # sans en faire sortir un ancien (sinon relecture chaque nuit).
            selected = [f for f in files if f.date >= self._cutoff]
        live = selected[-1] if selected else None
        live_name = live.name if live else None
        snap = {f.name: (f.size, f.mtime, f.ino) for f in selected if f.name != live_name}

        if (live_name != self._live and self._live in snap
                and set(snap) == set(self._snapshot) | {self._live}
                and all(snap[k] == v for k, v in self._snapshot.items())
                and snap[self._live][0] >= self._pos
                and snap[self._live][2] == self._live_ino):
            # Un fichier plus récent est apparu (minuit, ou fichier déposé) et
            # rien d'autre n'a bougé, l'ancien vivant compris (ni raccourci ni
            # remplacé) : on finit l'ancien vivant, on enchaîne sans relecture.
            with self.lock:
                self._read_new()
                self._flush_buffer()
                self._snapshot = snap
                self._start_file(live)
                self._read_new()
            return

        changed = (snap != self._snapshot or live_name != self._live
                   or (live is not None and (live.size < self._pos
                                             or live.ino != self._live_ino)))
        if changed:
            # Anti-rebond : un fichier en cours de copie grossit à chaque poll.
            # On ne relit que lorsque le dossier est stable d'un poll à l'autre.
            key = (tuple(sorted(snap.items())), live_name,
                   live.ino if live else None)
            if self._pending != key:
                self._pending = key
                return
            self._pending = None
            self.rebuild()
            return

        self._pending = None
        with self.lock:
            self._read_new()

    # ─── Résumé pour l'API ───────────────────────────────────────────────────
    def summary(self):
        with self.lock:
            return self._summary_unlocked()

    def _summary_unlocked(self):
        now = time.time()
        log_files = self.files_info()
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
                'implicit': sess.get('implicit', False),
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
            'log_dir': self.log_dir,
            'log_files': log_files,
            'log_size': sum(f['size'] for f in log_files if f['loaded']),
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

            # + les jours présents sur disque mais pas (encore) chargés : ils
            # restent sélectionnables, leur fichier est lu à la demande.
            days = sorted({s['day'] for s in shows} |
                          {sess['start'].strftime('%Y-%m-%d')
                           for sess in self.sessions}
                          | ({self.cur_session['start'].strftime('%Y-%m-%d')}
                             if self.cur_session else set())
                          | {f.date for f in self.list_files()})
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
                                       'down_from': None,
                                       'ws_in': 0, 'last_in': None})
            # Noms de matériel inconnus visés par le scénario : {nom: {count, first, last}}
            missing_hw = defaultdict(lambda: {'count': 0, 'first': None, 'last': None})

            # État au début de la période. Un échec de connexion n'est logué
            # qu'une fois par panne : un appareil tombé la VEILLE et jamais
            # revenu n'a aucune trace aujourd'hui, et compterait 0 s hors
            # ligne. On relit donc fail/connect d'avant la période pour savoir
            # qui est encore à terre quand elle commence.
            # Sessions Unity (epoch). Hors session, l'état d'un appareil est
            # INCONNU, pas « hors ligne » : Unity fermé entre 9 h 32 et 12 h 26
            # ne fait pas 3 h de panne. Le temps hors ligne n'est compté que
            # pendant qu'Unity tourne, et une panne ne traverse pas une
            # fermeture d'Unity (au redémarrage, le premier échec est relogué).
            sess_iv = []
            for sess in self.sessions + ([self.cur_session] if self.cur_session else []):
                a = sess['start'].timestamp()
                b = (sess['end'] or self.last_dt or sess['start']).timestamp()
                if b > a:
                    sess_iv.append((a, b))
            sess_iv.sort()

            def in_sessions(a, b):
                """Durée de [a, b] passée en session Unity."""
                tot = 0.0
                for sa, sb in sess_iv:
                    lo, hi = max(a, sa), min(b, sb)
                    if hi > lo:
                        tot += hi - lo
                return tot

            def session_at(ts):
                for sa, sb in sess_iv:
                    if sa <= ts <= sb:
                        return (sa, sb)
                return None

            if ranges:
                t_first = ranges[0][0]
                carried = {}            # nom -> début de la panne encore ouverte
                cur_sess = session_at(t_first)
                if cur_sess:            # seule la session en cours à t_first compte
                    for ts, kind, a, _b in recs:
                        if ts >= t_first:
                            break
                        if ts < cur_sess[0]:
                            continue
                        if kind == 'fail':
                            carried.setdefault(a, ts)
                        elif kind == 'connect':
                            carried.pop(a, None)
                for name, since in carried.items():
                    e = esp[name]
                    e['down_since'] = t_first
                    e['down_from'] = since
                    e['outages'] += 1
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
                        if b:                              # 0 = durée non mesurée (Yeelight)
                            e['connect_ms'].append(b)
                        if e['down_since'] is not None:   # fin de panne
                            e['downtime'] += in_sessions(e['down_since'], ts)
                            e['down_since'] = None
                    elif kind == 'fail':
                        e = esp[a]
                        e['failures'] += 1
                        e['failure_last_cause'] = b
                        esp_hour[a]['fail'][datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H')] += 1
                        if e['down_since'] is None:       # début de panne
                            e['down_since'] = ts
                            e['down_from'] = ts
                            e['outages'] += 1
                    elif kind == 'missing_hw':
                        mh = missing_hw[a]
                        mh['count'] += 1
                        mh['first'] = mh['first'] or ts
                        mh['last'] = ts
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
                    downtime += in_sessions(e['down_since'], range_end)
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
                    # Toujours hors ligne à la fin de la période, et depuis quand
                    # (peut être AVANT la période : panne de la veille).
                    'down_open': e['down_since'] is not None,
                    'down_since': (datetime.fromtimestamp(e['down_from']).isoformat()
                                   if e['down_from'] else None),
                    # A déjà répondu au moins une fois dans tout le journal :
                    # distingue une panne (il marchait) d'un appareil jamais
                    # branché ou inexistant.
                    'ever_connected': (self.esp[name]['connects'] > 0) if name in self.esp else False,
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
                'missing_hardware': [
                    {'name': n, 'count': mh['count'],
                     'first': datetime.fromtimestamp(mh['first']).isoformat(),
                     'last': datetime.fromtimestamp(mh['last']).isoformat()}
                    for n, mh in sorted(missing_hw.items(), key=lambda kv: -kv[1]['count'])],
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


# ─── Migration : découper un ancien journal unique en fichiers par jour ──────
def iter_day_lines(path):
    """Itère (jour 'AAAA-MM-JJ', ligne) sur un ancien journal unique
    (scenario_console.txt ou archive scenario_console_<date>_<HHMM>.txt).
    La date vient des en-têtes SESSION ; une heure qui recule de plus de 12 h
    = minuit passé, un marqueur SUITE SESSION est alors inséré (comme Unity
    le fait maintenant à la bascule de fichier)."""
    cur_date = None
    last_secs = None            # heure (en secondes) de la dernière ligne horodatée
    pending = []                # lignes lues avant le premier en-tête
    with open(path, 'rb') as f:
        for raw in f:
            line = raw.decode('utf-8', 'replace').rstrip('\r\n').lstrip('﻿')
            m = (RE_SESSION.match(line) or RE_SESSION_END.match(line)
                 or RE_SESSION_CONT.match(line))
            if m:
                cur_date = datetime.strptime(m.group(1), '%Y-%m-%d').date()
                hh, mm, ss = (int(x) for x in m.group(2).split(':'))
                last_secs = hh * 3600 + mm * 60 + ss
                for p in pending:
                    yield cur_date.isoformat(), p
                pending = []
                yield cur_date.isoformat(), line
                continue
            secs = None
            m = RE_LINE.match(line)
            if m:
                secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
            else:
                m = RE_LOOP.match(line)
                if m:
                    hh, mm, ss = (int(x) for x in m.group(2).split(':'))
                    secs = hh * 3600 + mm * 60 + ss
            if cur_date is None:
                pending.append(line)
                continue
            if secs is not None:
                if last_secs is not None and secs < last_secs - 12 * 3600:
                    cur_date += timedelta(days=1)
                    yield (cur_date.isoformat(),
                           f"========== SUITE SESSION {cur_date.isoformat()} "
                           f"{secs // 3600:02d}:{secs % 3600 // 60:02d}:{secs % 60:02d} ==========")
                last_secs = secs
            yield cur_date.isoformat(), line
    if pending:
        # Aucun en-tête dans tout le fichier : daté d'après sa dernière écriture.
        d = datetime.fromtimestamp(os.path.getmtime(path)).date().isoformat()
        for p in pending:
            yield d, p


def split_legacy(paths, out_dir, force=False):
    """Répartit un ou plusieurs anciens journaux dans
    out_dir/scenario_console_<jour>.txt. Refuse d'écrire dans un fichier-jour
    déjà présent (doublons garantis), sauf `force` (ajout en fin).
    Retourne (lignes écrites par jour, jours déjà présents)."""
    paths = [os.path.abspath(p) for p in paths]
    # Passe 1 : quels jours ? Pour détecter les collisions avant d'écrire.
    dates = set()
    for p in paths:
        for d, _line in iter_day_lines(p):
            dates.add(d)
    targets = {d: os.path.join(out_dir, f'{FILE_PREFIX}{d}.txt') for d in dates}
    existing = sorted(d for d, t in targets.items() if os.path.exists(t))
    if existing and not force:
        return Counter(), existing
    os.makedirs(out_dir, exist_ok=True)
    handles, counts = {}, Counter()
    try:
        for p in paths:
            for d, line in iter_day_lines(p):
                h = handles.get(d)
                if h is None:
                    h = handles[d] = open(targets[d], 'ab')
                h.write(line.encode('utf-8') + b'\n')
                counts[d] += 1
    finally:
        for h in handles.values():
            h.close()
    return counts, existing


def _main(argv):
    here = os.path.dirname(os.path.abspath(__file__))
    default_dir = os.path.normpath(os.path.join(here, '..', 'Logs'))

    if argv and argv[0] == '--split':
        force = '--force' in argv
        rest = [a for a in argv[1:] if a != '--force']
        out_dir = None
        if '--out' in rest:
            i = rest.index('--out')
            out_dir = rest[i + 1] if i + 1 < len(rest) else None
            del rest[i:i + 2]
        if not rest:
            print('usage : loganalyzer.py --split ancien.txt [...] [--out dossier] [--force]',
                  file=sys.stderr)
            return 2
        out_dir = os.path.abspath(out_dir or default_dir)
        counts, existing = split_legacy(rest, out_dir, force=force)
        if existing and not force:
            print('Fichiers-jours déjà présents dans ' + out_dir + ' : '
                  + ', '.join(existing), file=sys.stderr)
            print('Rien écrit. Retire-les avant, ou --force pour ajouter à leur suite.',
                  file=sys.stderr)
            return 1
        for d in sorted(counts):
            print(f'{FILE_PREFIX}{d}.txt : {counts[d]} lignes')
        print(f'-- {sum(counts.values())} lignes réparties sur {len(counts)} jour(s) '
              f'dans {out_dir} --', file=sys.stderr)
        return 0

    log_dir = argv[0] if argv else default_dir
    analyzer = LogAnalyzer(os.path.normpath(log_dir))
    t0 = time.time()
    analyzer.rebuild()
    out = analyzer.summary()
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"\n-- {len(out['log_files'])} fichier(s)-jour, {out['total_lines']} lignes "
          f"en {time.time() - t0:.2f}s --", file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(_main(sys.argv[1:]))
