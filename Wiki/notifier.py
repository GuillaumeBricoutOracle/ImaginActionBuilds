#!/usr/bin/env python3
"""Alertes et emails du dashboard Alice.

- AlertManager : évalue des règles sur le résumé de LogAnalyzer + le registre
  ESP, maintient la liste des alertes actives (avec cooldown anti-spam) et
  envoie les mails correspondants.
- Emails via SMTP (Gmail + mot de passe d'application conseillé), configurés
  dans dashboard_config.json (jamais commité — voir dashboard_config.example.json).
- Rapport quotidien envoyé à heure fixe.

Tous les envois se font dans un thread dédié : jamais bloquant, et silencieux
si pas d'internet (l'alerte reste visible sur le dashboard).
"""

import json
import os
import re
import smtplib
import subprocess
import threading
import time
from collections import deque
from datetime import datetime
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_DIR, 'dashboard_config.json')
# Compteurs de passages hors ligne : conservés sur disque pour survivre à un
# redémarrage du serveur (sinon « combien de fois » repart de zéro).
OFFLINE_STATS_PATH = os.path.join(_DIR, 'offline_stats.json')
# Journal borné : ~5000 épisodes = des années d'exploitation.
MAX_OFFLINE_EPISODES = 5000
# Un spectacle dont la durée s'écarte de la médiane au-delà de ce pourcentage
# est signalé « hors norme » dans le rapport.
SHOW_DEVIATION_PCT = 15


def _fmt_duration(seconds):
    s = int(seconds or 0)
    if s < 60:
        return f'{s} s'
    if s < 3600:
        return f'{s // 60} min {s % 60:02d} s'
    return f'{s // 3600} h {(s % 3600) // 60:02d} min'

DEFAULT_CONFIG = {
    'email_enabled': False,
    # Mails pour les alertes temps réel (ESP muet, Unity crashé...). À False,
    # seul le rapport quotidien part par mail ; les alertes restent visibles
    # sur le dashboard.
    'alert_emails': False,
    'smtp': {'host': 'smtp.gmail.com', 'port': 465, 'user': '', 'app_password': ''},
    'recipients': [],
    'daily_report_time': '22:00',
    'alert_cooldown_min': 30,
    # Dossier des binaires OTA produits par compilesketchs.py (1 bin par ESP)
    'firmware_dir': r'C:\Users\Admin\Documents\GitHub\Esp32\output-sketchs\ota',
    'thresholds': {
        'esp_silence_min': 10,        # ESP sans activité pendant X min (session ouverte)
        'loop_drift_pct': 20,         # dérive de durée de boucle vs médiane
        'log_frozen_s': 180,          # (historique, plus utilisé par défaut)
        'event_stuck_min_s': 300,     # durée mini avant de suspecter un event bloqué
    },
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    try:
        with open(CONFIG_PATH, encoding='utf-8') as f:
            user_cfg = json.load(f)
        for key, value in user_cfg.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
    except FileNotFoundError:
        pass
    except Exception as exc:
        print(f'[notifier] dashboard_config.json illisible : {exc}')
    return cfg


_unity_check = {'ts': 0.0, 'running': True}

def unity_process_running():
    """True si un process Unity (Editor ou build) tourne. Caché 30s."""
    now = time.time()
    if now - _unity_check['ts'] < 30:
        return _unity_check['running']
    running = True
    try:
        out = subprocess.run(['tasklist', '/FO', 'CSV', '/NH'],
                             capture_output=True, text=True, timeout=10).stdout
        low = out.lower()
        running = ('unity' in low) or ('aliceunity' in low) or ('imaginaction' in low)
    except Exception:
        running = True   # dans le doute, pas de fausse alerte
    _unity_check.update(ts=now, running=running)
    return running


def hardware_digest(summary):
    """Tri du matériel d'une période (résultat de stats_range enrichi par le
    serveur : scenario_scope, known_hardware). Partagé par le rapport du soir
    et la Vue d'ensemble du dashboard : même logique, mêmes mots.

    Trois cas, du plus grave au moins grave :
     - pannes : appareils qui ont fonctionné puis décroché (revenus ou non) ;
     - jamais joignables : appareils que le scénario utilise et qu'Unity n'a
       pas réussi à joindre une seule fois de toute la période ;
     - noms inconnus : le scénario vise un appareil absent de la liste du
       matériel (faute de frappe, renommage) ; l'action est jetée sans bruit.
    Seuls les appareils du scénario comptent : un appareil hors scénario (ou le
    boîtier de contrôle, suivi à part) n'empêche aucun spectacle.
    """
    esp = summary.get('esp') or {}
    scope = set(summary.get('scenario_scope') or [])
    known = summary.get('known_hardware') or []
    house = [str(t).lower() for t in (summary.get('house_tokens') or [])]
    # Sans spectacle terminé sur la période, rien à évaluer : les échecs d'une
    # session d'éditeur où rien n'est branché ne sont pas des pannes.
    if not ((summary.get('loops') or {}).get('count') or 0):
        return {'outages': [], 'unreachable': [], 'missing': [], 'still_down': 0,
                'lost_total': 0, 'problems': False, 'no_shows': True}
    try:
        range_start = datetime.fromisoformat((summary.get('ranges') or [[None]])[0][0])
    except Exception:
        range_start = None

    def since(e):
        """« depuis 22:40 », ou « depuis le 03/10 22:40 » si la panne a
        commencé avant la période (la veille)."""
        raw = e.get('down_since')
        if not raw:
            return '', False
        try:
            dt = datetime.fromisoformat(raw)
        except Exception:
            return '', False
        if range_start and dt < range_start:
            return f'depuis le {dt.strftime("%d/%m %H:%M")}', True
        return f'depuis {dt.strftime("%H:%M")}', False

    def is_subseq(short, long_):
        it = iter(long_)
        return all(ch in it for ch in short)

    def lookalike(name):
        """Nom connu dont `name` est une version tronquée (ex. « Servo_ » pour
        « Servo_Multi_ ») : c'est presque toujours la bonne cible."""
        cands = [k for k in known if len(k) > len(name) and is_subseq(name, k)]
        return min(cands, key=len) if cands else None

    outages, unreachable = [], []
    for name, e in esp.items():
        if not ((e.get('downtime_s') or 0) > 0 or e.get('down_open')):
            continue
        if scope and name not in scope:
            continue
        if re.search(r'_(PC|Controller)$', name, re.I):   # suivis à part, pas des appareils de scène
            continue
        if house and not all(t in name.lower() for t in house):
            continue
        since_txt, before = since(e)
        row = {'name': name, 'since_txt': since_txt, 'since_before_range': before,
               'downtime_s': e.get('downtime_s') or 0, 'outages': e.get('outages') or 0,
               'open': bool(e.get('down_open')), 'lost': e.get('lost') or 0}
        # A marché un jour (dans la période ou avant) = panne ; n'a jamais
        # répondu de tout le journal = jamais branché ou inexistant.
        worked = (e.get('connects') or 0) > 0 or (e.get('ws_out') or 0) > 0 \
            or e.get('ever_connected')
        (outages if worked else unreachable).append(row)
    outages.sort(key=lambda r: (-r['open'], -r['downtime_s']))
    unreachable.sort(key=lambda r: (-r['lost'], r['name']))

    missing = [{'name': m['name'], 'count': m['count'], 'lookalike': lookalike(m['name'])}
               for m in (summary.get('missing_hardware') or [])]

    return {
        'outages': outages,
        'unreachable': unreachable,
        'missing': missing,
        'still_down': sum(1 for r in outages if r['open']),
        'lost_total': sum(r['lost'] for r in outages + unreachable),
        'problems': bool(outages or unreachable or missing),
        'no_shows': False,
    }


class Mailer:
    def __init__(self):
        self._queue = deque()
        self._cv = threading.Condition()
        self.last_result = None   # {'ts', 'ok', 'detail', 'subject'}
        threading.Thread(target=self._worker, daemon=True).start()

    def send(self, subject, html_body, images=None):
        """images = {cid: octets PNG} référencés dans le HTML par src="cid:<cid>"."""
        with self._cv:
            self._queue.append((subject, html_body, images or {}))
            self._cv.notify()

    def _worker(self):
        while True:
            with self._cv:
                while not self._queue:
                    self._cv.wait()
                subject, body, images = self._queue.popleft()
            self._send_now(subject, body, images)

    def _send_now(self, subject, html_body, images=None):
        cfg = load_config()
        smtp = cfg.get('smtp', {})
        recipients = cfg.get('recipients', [])
        result = {'ts': time.time(), 'subject': subject}
        if not cfg.get('email_enabled') or not smtp.get('user') or not recipients:
            result.update(ok=False, detail='email désactivé ou config incomplète')
            self.last_result = result
            return
        try:
            if images:
                # multipart/related : l'image voyage DANS le mail (Content-ID),
                # pas en pièce jointe séparée ni via une URL que le client
                # bloquerait. C'est le seul format d'image fiable en mail.
                msg = MIMEMultipart('related')
                msg.attach(MIMEText(html_body, 'html', 'utf-8'))
                for cid, data in images.items():
                    part = MIMEImage(data, _subtype='png')
                    part.add_header('Content-ID', f'<{cid}>')
                    part.add_header('Content-Disposition', 'inline', filename=f'{cid}.png')
                    msg.attach(part)
            else:
                msg = MIMEText(html_body, 'html', 'utf-8')
            msg['Subject'] = subject
            msg['From'] = smtp.get('from') or smtp['user']
            msg['To'] = ', '.join(recipients)
            with smtplib.SMTP_SSL(smtp.get('host', 'smtp.gmail.com'),
                                  int(smtp.get('port', 465)), timeout=20) as server:
                server.login(smtp['user'], smtp['app_password'])
                server.sendmail(msg['From'], recipients, msg.as_string())
            result.update(ok=True, detail=f'envoyé à {len(recipients)} destinataire(s)')
        except Exception as exc:
            result.update(ok=False, detail=str(exc)[:300])
        self.last_result = result


class AlertManager:
    """Évalue les règles et garde l'historique des alertes (max 200)."""

    def __init__(self, mailer, broadcast=None):
        self.mailer = mailer
        self.broadcast = broadcast or (lambda event, payload: None)
        self.alerts = deque(maxlen=200)    # {id, ts, key, severity, title, detail}
        self.lock = threading.Lock()
        self._alert_seq = 0
        self._last_fired = {}              # key -> wall time (cooldown)
        self._seen_error_count = None
        self._seen_lost = {}               # esp -> lost count déjà signalé
        self._known_sigs = None            # signatures connues (None = pas amorcé)
        self._last_report_day = None

        # Journal des passages hors ligne : [{name, scenario, start, end|None}]
        # chronologique. Les agrégats (combien de fois, combien de temps) en
        # sont dérivés, et le rapport d'un jour y pioche ses épisodes.
        self.episodes = []
        self._pending = []                 # transitions à annoncer, hors lock
        self._load_offline()

    # ─── Émission ────────────────────────────────────────────────────────────
    def _fire(self, key, severity, title, detail, email=True, once=False):
        cfg = load_config()
        cooldown = float(cfg.get('alert_cooldown_min', 30)) * 60
        now = time.time()
        if once and key in self._last_fired:
            return                       # une seule fois, jamais répétée
        if now - self._last_fired.get(key, 0) < cooldown:
            return
        self._last_fired[key] = now
        with self.lock:
            self._alert_seq += 1
            alert = {'id': self._alert_seq, 'ts': now,
                     'iso': datetime.now().isoformat(timespec='seconds'),
                     'key': key, 'severity': severity, 'title': title, 'detail': detail}
            self.alerts.append(alert)
        self.broadcast('alert', alert)
        if email and cfg.get('alert_emails'):
            body = (f'<h3>{title}</h3><p>{detail}</p>'
                    f'<p style="color:#888">ImaginAction Supervision — '
                    f'{alert["iso"]}</p>')
            self.mailer.send(f'[ImaginAction][{severity.upper()}] {title}', body)

    def dismiss(self, alert_id):
        """Supprime une alerte de la liste (bouton × du dashboard)."""
        with self.lock:
            kept = [a for a in self.alerts if a.get('id') != alert_id]
            removed = len(self.alerts) - len(kept)
            self.alerts.clear()
            self.alerts.extend(kept)
        return removed > 0

    def clear_all(self):
        with self.lock:
            self.alerts.clear()

    # ─── Règle unique : les passages hors ligne ──────────────────────────────
    # Les anciennes règles (« ESP muet » basée sur le volume de messages reçus,
    # et « Unity arrêté ») ont été retirées : la première ne couvrait en
    # pratique qu'un appareil sur tout le parc, la seconde était neutralisée
    # par la simple présence d'Unity Hub dans la liste des process.
    #
    # On ne suit plus qu'une chose, celle qui est demandée : un appareil qui
    # passe hors ligne PENDANT un spectacle. Combien de fois, et combien de
    # temps. Chaque épisode est journalisé individuellement, avec le scénario
    # en cours, pour que le rapport d'un jour puisse les lister.
    def track_offline(self, statuses, scenario=None):
        """statuses = {nom: statut effectif} des appareils SOUS SURVEILLANCE.

        Le dictionnaire est vide hors scénario, et limité aux appareils du
        scénario en cours pendant une lecture : un décrochage qui n'empêche
        aucun spectacle de se jouer n'a pas à polluer les compteurs.

        Tout épisode ouvert sur un appareil absent du dictionnaire est clos :
        la fin d'un scénario arrête le décompte du temps hors ligne.
        """
        now = time.time()
        changed = False

        with self.lock:
            open_by_name = {ep['name']: ep for ep in self.episodes if ep['end'] is None}

            for name, status in statuses.items():
                ep = open_by_name.get(name)

                if status == 'offline' and ep is None:
                    ep = {'name': name, 'scenario': scenario or '', 'start': now, 'end': None}
                    self.episodes.append(ep)
                    open_by_name[name] = ep
                    changed = True
                    self._pending.append(('off', name, self._count_locked(name), None))

                elif status != 'offline' and ep is not None:
                    ep['end'] = now
                    changed = True
                    self._pending.append(('on', name, self._count_locked(name), now - ep['start']))

            # Hors périmètre (scénario fini, ou appareil absent de ce scénario) :
            # on fige l'épisode en cours au lieu de le laisser courir.
            for name, ep in open_by_name.items():
                if ep['end'] is None and name not in statuses:
                    ep['end'] = now
                    changed = True

            if len(self.episodes) > MAX_OFFLINE_EPISODES:
                del self.episodes[:len(self.episodes) - MAX_OFFLINE_EPISODES]

        # Émission hors du lock : _fire reprend le lock pour la liste d'alertes.
        pending, self._pending = self._pending, []
        for kind, name, count, duration in pending:
            if kind == 'off':
                self._fire(f'offline-{name}', 'warn', f'{name} est hors ligne',
                           f'{count}\u1d49 passage hors ligne pendant un spectacle.'
                           if count > 1 else 'Premier passage hors ligne pendant un spectacle.')
            else:
                self._fire(f'back-{name}-{count}', 'info', f'{name} est revenu en ligne',
                           f'Absent pendant {_fmt_duration(duration)}.')

        if changed:
            self._save_offline()

    def _count_locked(self, name):
        return sum(1 for ep in self.episodes if ep['name'] == name)

    def offline_stats(self):
        """Agrégats pour la vue d'ensemble : {nom: {count, total_s, ...}}."""
        now = time.time()
        with self.lock:
            out = {}
            for ep in self.episodes:   # chronologique
                st = out.setdefault(ep['name'], {
                    'count': 0, 'total_s': 0.0, 'currently_off': False,
                    'current_s': 0.0, 'last_duration_s': None, 'last_offline_at': None})
                st['count'] += 1
                st['last_offline_at'] = ep['start']
                if ep['end'] is None:
                    st['currently_off'] = True
                    st['current_s'] = now - ep['start']
                    st['total_s'] += st['current_s']
                else:
                    duration = ep['end'] - ep['start']
                    st['total_s'] += duration
                    st['last_duration_s'] = duration
            return out

    def offline_episodes(self, day_str):
        """Épisodes dont le début tombe le jour donné (AAAA-MM-JJ), pour le rapport."""
        now = time.time()
        with self.lock:
            eps = [dict(ep) for ep in self.episodes
                   if datetime.fromtimestamp(ep['start']).strftime('%Y-%m-%d') == day_str]
        for ep in eps:
            ep['open'] = ep['end'] is None
            ep['duration_s'] = (ep['end'] if ep['end'] is not None else now) - ep['start']
        return eps

    def reset_offline_stats(self):
        with self.lock:
            self.episodes.clear()
        self._save_offline()

    # Persistance : le journal survit à un redémarrage du serveur, sinon
    # « combien de fois » repartirait de zéro à chaque relance.
    def _save_offline(self):
        try:
            tmp = OFFLINE_STATS_PATH + '.tmp'
            with self.lock:
                data = {'episodes': [dict(ep) for ep in self.episodes]}
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f)
            os.replace(tmp, OFFLINE_STATS_PATH)
        except Exception as exc:
            print(f'[notifier] sauvegarde des passages hors ligne : {exc}')

    def _load_offline(self):
        try:
            with open(OFFLINE_STATS_PATH, encoding='utf-8') as f:
                data = json.load(f)
            eps = data.get('episodes') if isinstance(data, dict) else None
            if not isinstance(eps, list):
                return   # ancien format (agrégats sans horodatage) : on repart propre
            for ep in eps:
                if not (isinstance(ep, dict) and 'name' in ep and 'start' in ep):
                    continue
                if ep.get('end') is None:
                    # Épisode encore ouvert à l'arrêt du serveur : le temps où le
                    # serveur ne tournait pas ne compte pas, durée inconnue.
                    ep['end'] = ep['start']
                    ep['truncated'] = True
                self.episodes.append(ep)
        except FileNotFoundError:
            pass
        except Exception as exc:
            print(f'[notifier] lecture des passages hors ligne : {exc}')

    # ─── Rapport quotidien ───────────────────────────────────────────────────
    def maybe_daily_report(self, summary, image_provider=None):
        """image_provider : callable -> octets PNG de l'onglet Spectacles du
        jour, appelé seulement au moment de l'envoi (la capture est coûteuse)."""
        cfg = load_config()
        report_time = cfg.get('daily_report_time', '22:00')
        now = datetime.now()
        today = now.strftime('%Y-%m-%d')
        if self._last_report_day == today:
            return
        try:
            hh, mm = (int(x) for x in report_time.split(':'))
        except Exception:
            hh, mm = 22, 0
        if (now.hour, now.minute) < (hh, mm):
            return
        self._last_report_day = today
        png = None
        if image_provider:
            try:
                png = image_provider()
            except Exception as exc:
                print(f'[notifier] capture du rapport : {exc}')
        self.mailer.send(f'[ImaginAction] Rapport du {today}',
                         self.build_report_html(summary, f'du {today}',
                                                image_src='cid:spectacles' if png else None),
                         images={'spectacles': png} if png else None)

    def build_report_html(self, summary, date_label='', image_src=None):
        """Rapport HTML lisible, styles inline (compatible clients mail).

        Le cœur du rapport est une IMAGE : la capture de l'onglet Spectacles &
        Stats du jour (tuiles, graphique des durées avec anomalies en rouge et
        médiane en pointillés, activité par heure), telle que l'exploitant la
        voit dans le dashboard. image_src = 'cid:...' pour le mail, ou une URL
        pour l'onglet Rapports. S'y ajoutent le verdict et les passages hors
        ligne pendant les spectacles. Tout le reste (erreurs Unity, choix des
        visiteurs, boîtier, santé réseau) reste dans le dashboard.
        """
        loops = summary.get('loops') or {}
        shows = [s for s in (loops.get('recent') or []) if s.get('duration_s') is not None]
        med = loops.get('median_s')
        episodes = summary.get('offline_episodes') or []

        # ── Matériel, d'après le journal Unity (même tri que la Vue d'ensemble)
        hw = hardware_digest(summary)
        outages, unreachable, missing = hw['outages'], hw['unreachable'], hw['missing']
        still_down = hw['still_down']

        th = ('padding:6px 10px;text-align:left;font-size:11px;color:#777;'
              'text-transform:uppercase;letter-spacing:.04em;'
              'border-bottom:2px solid #ddd')
        td = 'padding:6px 10px;border-bottom:1px solid #eee;font-size:13px'
        red, amber, green = '#c62828', '#b26a00', '#2e7d32'

        # ── Spectacles hors norme : écart à la médiane ──────────────────────
        def deviation_pct(d):
            return (d - med) / med * 100.0 if med else 0.0

        anomalies = [s for s in shows if med and abs(deviation_pct(s['duration_s'])) > SHOW_DEVIATION_PCT]
        shorter = sum(1 for s in anomalies if s['duration_s'] < med)
        longer = len(anomalies) - shorter

        # ── Verdict ─────────────────────────────────────────────────────────
        problems = []
        if anomalies:
            parts = [x for x in ((f'{shorter} plus court(s)' if shorter else ''),
                                 (f'{longer} plus long(s)' if longer else '')) if x]
            problems.append(f'{len(anomalies)} spectacle(s) hors norme ({" / ".join(parts)})')
        if outages:
            problems.append(f'{len(outages)} appareil(s) en panne'
                            + (f' dont {still_down} toujours hors ligne' if still_down else ''))
        if unreachable:
            problems.append(f'{len(unreachable)} appareil(s) du scénario jamais joignable(s)')
        if missing:
            problems.append(f'{len(missing)} nom(s) de matériel inconnu(s) dans le scénario')
        if episodes:
            problems.append(f'{len(episodes)} passage(s) hors ligne pendant les spectacles')
        if not shows:
            verdict = 'Aucun spectacle joué.'
        elif not problems:
            verdict = f'✅ {len(shows)} spectacle(s), aucune anomalie.'
        else:
            verdict = '🔴 À vérifier : ' + ', '.join(problems) + '.'

        # Unity loggue le scénario de DÉMARRAGE ; celui réellement joué (reconnu
        # d'après les events) peut être un autre : on affiche les deux.
        scen_names = list(summary.get('scenario_names') or [])
        guess = os.path.splitext((summary.get('events') or {}).get('scenario_guess') or '')[0]
        if guess and guess not in scen_names:
            scen_names.append(f'{guess} (d\'après les events joués)')
        scen_line = (f'<p style="margin:2px 0 0;font-size:12.5px;color:#777">Scénario(s) : '
                     f'{", ".join(scen_names)}</p>') if scen_names else ''

        # ── Capture de l'onglet Spectacles & Stats du jour ──────────────────
        # Le rapport EST cette image : tuiles, graphique des durées (anomalies
        # en rouge, médiane en pointillés), activité par heure — exactement ce
        # que l'exploitant voit dans le dashboard. Produite côté serveur par un
        # navigateur headless. Si la capture échoue, on le dit plutôt que de
        # retomber sur un tableau de 125 lignes.
        if image_src:
            shows_html = (f'<h3 style="margin:22px 0 6px;font-size:15px">Spectacles &amp; stats du jour</h3>'
                          f'<img src="{image_src}" alt="Spectacles et statistiques du jour" '
                          f'style="display:block;width:100%;max-width:700px;border-radius:8px;'
                          f'border:1px solid #ddd">')
        elif shows:
            shows_html = (f'<h3 style="margin:22px 0 6px;font-size:15px">Spectacles &amp; stats du jour</h3>'
                          f'<p style="color:{amber}">Capture du dashboard indisponible (navigateur '
                          f'headless absent ou en échec) — {len(shows)} spectacle(s), médiane '
                          f'{_fmt_duration(med) if med else "—"}.</p>')
        else:
            shows_html = ''

        # ── Section Matériel ────────────────────────────────────────────────
        def table(headers, rows):
            head = ''.join(f'<th style="{th}">{h}</th>' for h in headers)
            return f'<table style="border-collapse:collapse;width:100%"><tr>{head}</tr>{rows}</table>'

        hw_parts = []
        if outages:
            rows = ''
            for o in outages:
                state, color = ('toujours hors ligne', red) if o['open'] else ('revenu', green)
                n_out = f' ({o["outages"]} pannes)' if o['outages'] > 1 else ''
                rows += (f'<tr><td style="{td};font-weight:600">{o["name"]}</td>'
                         f'<td style="{td}">{o["since_txt"]}</td>'
                         f'<td style="{td}">{_fmt_duration(o["downtime_s"])}{n_out}</td>'
                         f'<td style="{td};color:{color};font-weight:600">{state}</td>'
                         f'<td style="{td};color:{red if o["lost"] else "#1a1a1a"}">{o["lost"]}</td></tr>')
            hw_parts.append('<h4 style="margin:14px 0 4px;font-size:13px;color:#555">Pannes</h4>'
                            + table(['Appareil', 'Début', 'Hors ligne', 'État', 'Actions perdues'], rows))
        if unreachable:
            # Une ligne par appareil qui a fait perdre des actions ; ceux que le
            # scénario déclare sans jamais les solliciter tiennent en une phrase.
            rows, idle = '', []
            for u in unreachable:
                if not u['lost']:
                    idle.append(u['name'])
                    continue
                rows += (f'<tr><td style="{td};font-weight:600">{u["name"]}</td>'
                         f'<td style="{td}">{u["since_txt"]}</td>'
                         f'<td style="{td};color:{red}">{u["lost"]} action(s) perdue(s)</td></tr>')
            block = ('<h4 style="margin:14px 0 4px;font-size:13px;color:#555">Jamais joignables '
                     '(utilisés par le scénario)</h4>')
            if rows:
                block += table(['Appareil', 'Hors ligne', 'Impact'], rows)
            if idle:
                block += (f'<p style="margin:6px 0 0;font-size:12.5px;color:#777">'
                          f'{"Également injoignables" if rows else "Injoignables"}, mais aucune '
                          f'action ne leur a été envoyée : {", ".join(sorted(idle))}.</p>')
            hw_parts.append(block)
        if missing:
            rows = ''
            for mh in missing:
                hint = (f'existe sous le nom <b>{mh["lookalike"]}</b>' if mh['lookalike']
                        else 'aucun appareil de ce nom dans la liste du matériel')
                rows += (f'<tr><td style="{td};font-weight:600">{mh["name"]}</td>'
                         f'<td style="{td};color:{red}">{mh["count"]} action(s) jetée(s)</td>'
                         f'<td style="{td}">{hint}</td></tr>')
            hw_parts.append('<h4 style="margin:14px 0 4px;font-size:13px;color:#555">Noms inconnus '
                            'dans le scénario</h4>'
                            + table(['Nom dans le scénario', 'Impact', 'Diagnostic'], rows)
                            + '<p style="margin:4px 0 0;font-size:12px;color:#777">À corriger dans le '
                              'scénario (ou à ajouter à la liste du matériel si l\'appareil existe).</p>')

        title_hw = '<h3 style="margin:22px 0 6px;font-size:15px">Matériel</h3>'
        if hw_parts:
            hardware_html = title_hw + ''.join(hw_parts)
        elif hw.get('no_shows'):
            hardware_html = (title_hw + '<p style="color:#777">Aucun spectacle terminé : '
                             'matériel non évalué.</p>')
        else:
            hardware_html = (title_hw + f'<p style="color:{green}">Aucune panne, tous les appareils '
                             f'du scénario joignables, aucun nom inconnu.</p>')

        # ── Passages hors ligne pendant les spectacles ──────────────────────
        off_rows = ''
        recap = {}
        for ep in episodes:
            r = recap.setdefault(ep['name'], {'count': 0, 'total_s': 0.0})
            r['count'] += 1
            r['total_s'] += ep.get('duration_s') or 0.0
            if ep.get('truncated'):
                dur_txt, color = 'durée inconnue (serveur redémarré)', amber
            elif ep.get('open'):
                dur_txt, color = f'encore hors ligne ({_fmt_duration(ep.get("duration_s"))})', red
            else:
                dur_txt, color = _fmt_duration(ep.get('duration_s')), '#1a1a1a'
            start = datetime.fromtimestamp(ep['start']).strftime('%H:%M:%S')
            off_rows += (f'<tr><td style="{td}">{start}</td>'
                         f'<td style="{td};font-weight:600">{ep["name"]}</td>'
                         f'<td style="{td};color:#777">{ep.get("scenario") or ""}</td>'
                         f'<td style="{td};color:{color}">{dur_txt}</td></tr>')

        recap_rows = ''.join(
            f'<tr><td style="{td};font-weight:600">{name}</td>'
            f'<td style="{td};color:{red if r["count"] > 1 else "#1a1a1a"}">{r["count"]}</td>'
            f'<td style="{td}">{_fmt_duration(r["total_s"])}</td></tr>'
            for name, r in sorted(recap.items(), key=lambda kv: (-kv[1]['count'], -kv[1]['total_s'])))

        title_off = ('<h3 style="margin:22px 0 6px;font-size:15px">Passages hors ligne '
                     'pendant les spectacles</h3>')
        if off_rows:
            offline_html = (title_off
                            + f'<table style="border-collapse:collapse;width:100%">'
                            f'<tr><th style="{th}">Appareil</th><th style="{th}">Passages</th>'
                            f'<th style="{th}">Temps hors ligne cumulé</th></tr>{recap_rows}</table>'
                            f'<h4 style="margin:14px 0 4px;font-size:13px;color:#555">Détail</h4>'
                            f'<table style="border-collapse:collapse;width:100%">'
                            f'<tr><th style="{th}">Heure</th><th style="{th}">Appareil</th>'
                            f'<th style="{th}">Scénario</th><th style="{th}">Durée</th></tr>'
                            f'{off_rows}</table>')
        elif hw_parts:
            # Le journal Unity montre des problèmes matériel : ne pas afficher
            # un « tout va bien » contradictoire (ce suivi-ci n'existe que si le
            # dashboard tournait pendant les spectacles).
            offline_html = ''
        else:
            offline_html = (title_off
                            + f'<p style="color:{green}">Aucun appareil passé hors ligne pendant un '
                            f'spectacle. 🎉</p>')

        return f"""<div style="font-family:system-ui,'Segoe UI',sans-serif;max-width:700px;
margin:0 auto;color:#1a1a1a;background:#fff;padding:8px 4px">
<h2 style="font-size:19px;margin:0 0 2px">ImaginAction — Rapport {date_label or 'quotidien'}</h2>
{scen_line}
<p style="margin:6px 0 14px;font-size:14px">{verdict}</p>
{shows_html}
{hardware_html}
{offline_html}
<p style="color:#999;font-size:11px;margin-top:24px">Généré automatiquement par
ImaginAction Supervision.</p></div>"""
