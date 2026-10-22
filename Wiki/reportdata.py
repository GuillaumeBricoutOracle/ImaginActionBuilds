#!/usr/bin/env python3
"""Interprétation des logs pour le rapport quotidien : spectacles, choix, incidents ESP.

Tout est DÉDUIT des logs (aucune modification des logs). Un « spectacle » = les trois
scénarios d'un univers (entrée + central + scène). Les autres maisons n'arrivent à la
Forêt que par leur résumé BLE (remote_shows) : on les montre au niveau résumé.

Fonctions publiques :
  parse_scenario(name)              -> {'code','maison','part'} ou None
  load_scenario_index(dirs)         -> {event_id: {duration, default, branches, scenario}}
  compute(day, shows, records, esp, remote_shows, offline_episodes, scenario_index) -> dict
  render_html(data, date_label)     -> str (HTML email, styles en ligne)
"""
import glob
import json
import os
import re
import statistics
from datetime import datetime

UNIVERSE = {'FOR': 'Forêt', 'BAN': 'Banquet', 'CHA': 'Chaumière', 'PAL': 'Palais'}
PARTS = ['ENTREE', 'CENTRAL', 'SCENE']
PART_LABEL = {'ENTREE': 'Entrée', 'CENTRAL': 'Central', 'SCENE': 'Scène'}
GAP_MAX_S = 900          # au-delà, deux scénarios ne sont pas le même spectacle
SKIP_RATIO = 0.5         # scénario < moitié de sa durée médiane -> probablement écourté

_RE_SCEN = re.compile(r'_(FOR|BAN|CHA|PAL)_(ENTREE|CENTRAL|SCENE)', re.I)


def parse_scenario(name):
    if not name:
        return None
    m = _RE_SCEN.search(name.upper())
    if not m:
        return None
    return {'code': m.group(1), 'maison': UNIVERSE[m.group(1)], 'part': m.group(2)}


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ─────────────────────────────────────────────────────────── scénarios (choix)
def load_scenario_index(dirs):
    """Événements de choix de tous les scénarios : délai, branche par défaut, branches par boîtier."""
    idx = {}
    for d in dirs or ():
        if not d or not os.path.isdir(d):
            continue
        for path in glob.glob(os.path.join(d, '**', '*.json'), recursive=True):
            try:
                with open(path, encoding='utf-8-sig') as f:
                    data = json.load(f)
            except (OSError, ValueError):
                continue
            events = data.get('events') if isinstance(data, dict) else None
            if not isinstance(events, list):
                continue
            scen = os.path.splitext(os.path.basename(path))[0]
            for e in events:
                if not isinstance(e, dict) or not e.get('id'):
                    continue
                choice_acts = [a for a in (e.get('actions') or [])
                               if 'complete_event_if' in (a.get('action') or '')]
                if not choice_acts:
                    continue
                branches = {}
                for a in choice_acts:
                    o = a.get('options', {}) or {}
                    nxt = o.get('next_event_id')
                    if nxt:
                        branches[nxt] = o.get('sensor_hardware') or '?'
                idx[e['id']] = {
                    'duration': _num(e.get('duration')),
                    'default': e.get('next_event_id'),
                    'branches': branches,
                    'scenario': scen,
                }
    return idx


# ─────────────────────────────────────────────────────────── spectacles
def _spectacles(shows):
    """Groupe les scénarios (shows) en spectacles par univers. Un nouveau spectacle commence
    quand l'univers change, qu'un trou dépasse GAP_MAX_S, ou qu'une partie déjà jouée revient
    (gère la boucle infinie : chaque cycle entrée/central/scène = un spectacle)."""
    rows = []
    for s in shows:
        # scenario_detected = scénario RÉELLEMENT joué (déduit du 1er event) ; le nom loggué
        # au START est celui du départ de la playlist (souvent « entrée ») et ment après enchaînement.
        name = s.get('scenario_detected') or s.get('scenario')
        info = parse_scenario(name)
        if not info:
            continue
        s = dict(s, _scenario=name)
        rows.append((s, info))
    rows.sort(key=lambda r: r[0].get('start') or 0)

    specs = []
    cur = None
    for s, info in rows:
        start = s.get('start') or 0
        end = s.get('end') or start
        restart = cur and info['part'] in cur['parts_seen']
        gap = (start - cur['end']) if cur else 0
        if (cur is None or info['code'] != cur['code'] or gap > GAP_MAX_S or restart):
            cur = {'code': info['code'], 'maison': info['maison'], 'start': start, 'end': end,
                   'parts_seen': set(), 'scenarios': []}
            specs.append(cur)
        cur['end'] = max(cur['end'], end)
        cur['parts_seen'].add(info['part'])
        cur['scenarios'].append({
            'part': info['part'], 'scenario': s.get('_scenario') or s.get('scenario'),
            'start': start, 'end': end, 'duration': _num(s.get('duration_s')),
            'partial': bool(s.get('partial')), 'errors': s.get('errors', 0),
            'warns': s.get('warns', 0), 'lost': s.get('lost', 0),
            'first_event': s.get('first_event'),
        })
    # médiane par scénario, pour repérer les scénarios écourtés (skip probable)
    durs_by_scen = {}
    for sp in specs:
        for sc in sp['scenarios']:
            durs_by_scen.setdefault(sc['scenario'], []).append(sc['duration'])
    med = {k: statistics.median(v) for k, v in durs_by_scen.items() if v}
    for sp in specs:
        sp['complete'] = len(sp['parts_seen']) == 3
        sp['gap_before'] = None
        for sc in sp['scenarios']:
            m = med.get(sc['scenario'])
            sc['short'] = bool(m and sc['duration'] < SKIP_RATIO * m and sc['duration'] > 0)
    # écart entre spectacles successifs (même ou autre univers)
    for i in range(1, len(specs)):
        specs[i]['gap_before'] = round(specs[i]['start'] - specs[i - 1]['end'], 0)
    for sp in specs:
        sp.pop('parts_seen', None)
        sp['duration'] = round(sp['end'] - sp['start'], 0)
    return specs


# ─────────────────────────────────────────────────────────── choix
def _choices(records, scenario_index):
    """Issue de chaque événement de choix : boîtier utilisé ou défaut (délai écoulé), et le temps mis."""
    stats = {'total': 0, 'by_box': {}, 'default': 0, 'times': [], 'default_times': []}
    prev_evdur = {}    # dernier evdur (event_id -> durée) vu juste avant un fire
    for ts, kind, a, b in records:
        if kind == 'evdur':
            prev_evdur[a] = b
        elif kind == 'fire':
            outcome, choice_ev = a, b
            info = scenario_index.get(choice_ev)
            if not info:
                continue
            stats['total'] += 1
            t = prev_evdur.get(choice_ev)
            if outcome == info['default']:
                stats['default'] += 1
                if t is not None:
                    stats['default_times'].append(t)
            else:
                box = info['branches'].get(outcome, 'autre')
                stats['by_box'][box] = stats['by_box'].get(box, 0) + 1
                if t is not None:
                    stats['times'].append(t)
    return stats


# ─────────────────────────────────────────────────────────── incidents ESP
def _clean_cause(c):
    return (c or '').strip().rstrip('|—- ').strip()


def _esp_incidents(day_records, offline_episodes):
    """Calculé sur les records DU JOUR (les stats cumulées de l'analyseur couvrent 14 jours).
    'connect' (name, ms) et 'fail' (name, cause) sont horodatés dans les records.
    Déconnexion = connexion au-delà de la première dans la journée ; latence = les ms mesurés."""
    conns = {}    # name -> [ms, ...]
    fails = {}    # name -> nb
    cause = {}
    for _ts, kind, a, b in day_records:
        if kind == 'connect':
            conns.setdefault(a, []).append(b or 0)
        elif kind == 'fail':
            fails[a] = fails.get(a, 0) + 1
            cause[a] = b
    devices = []
    never = []
    all_ms = []
    total_reconnects = 0
    for name, ms_list in conns.items():
        cm = [v for v in ms_list if v]          # ms>0 = vraie mesure (les lampes Yeelight valent 0)
        reconns = max(0, len(ms_list) - 1)
        total_reconnects += reconns
        all_ms += cm
        devices.append({
            'name': name, 'reconnects': reconns,
            'lat_avg': round(sum(cm) / len(cm)) if cm else None,
            'lat_min': min(cm) if cm else None, 'lat_max': max(cm) if cm else None,
            'cause': _clean_cause(cause.get(name)) if reconns else '',
        })
    for name in fails:
        if name not in conns:
            never.append(name)
    # problèmes d'abord (déconnexions), puis latence la plus haute
    devices.sort(key=lambda d: (-d['reconnects'], -(d['lat_avg'] or 0), d['name']))
    episodes = [ep for ep in (offline_episodes or []) if ep.get('duration_s')]
    off_total = sum(ep['duration_s'] for ep in episodes)
    off_longest = max((ep['duration_s'] for ep in episodes), default=0)
    return {
        'devices': devices,
        'total_reconnects': total_reconnects,
        'never': sorted(never),
        'n_never': len(never),
        'lat_avg': round(sum(all_ms) / len(all_ms)) if all_ms else None,
        'lat_min': min(all_ms) if all_ms else None,
        'lat_max': max(all_ms) if all_ms else None,
        'offline_count': len(episodes),
        'offline_total_s': off_total,
        'offline_longest_s': off_longest,
    }


# ─────────────────────────────────────────────────────────── assemblage
def _day_window(day):
    try:
        start = datetime.strptime(day, '%Y-%m-%d')
        return start.timestamp(), (start.replace(hour=23, minute=59, second=59).timestamp() + 1)
    except (TypeError, ValueError):
        return 0, float('inf')


def compute(day, shows, records, esp, remote_shows=None, offline_episodes=None, scenario_index=None):
    day_shows = [s for s in (shows or []) if s.get('day') == day]
    t0, t1 = _day_window(day)
    day_records = [r for r in (records or []) if t0 <= r[0] < t1]
    specs = _spectacles(day_shows)
    complete = [sp for sp in specs if sp['complete']]
    partials = [sp for sp in specs if not sp['complete']]
    local_total = sum(sp['duration'] for sp in specs)

    remote = {}
    for s in (remote_shows or []):
        if _day_of_epoch(s.get('s')) != day:
            continue
        m = s.get('m', '?')
        r = remote.setdefault(m, {'n': 0, 'duration': 0, 'partial': 0, 'errors': 0})
        r['n'] += 1
        r['duration'] += int(s.get('du') or 0)
        r['partial'] += 1 if s.get('p') else 0
        r['errors'] += int(s.get('er') or 0)
    remote_total = sum(r['duration'] for r in remote.values())

    return {
        'day': day,
        'spectacles': specs,
        'n_complete': len(complete),
        'n_partial': len(partials),
        'local_total_s': local_total,
        'grand_total_s': local_total + remote_total,
        'choices': _choices(day_records, scenario_index or {}),
        'esp': _esp_incidents(day_records, offline_episodes),
        'remote': remote,
    }


def _day_of_epoch(ep):
    try:
        return datetime.fromtimestamp(int(ep)).strftime('%Y-%m-%d')
    except (TypeError, ValueError, OSError):
        return None


# ─────────────────────────────────────────────────────────── rendu HTML
def _fmt_dur(s):
    s = int(s or 0)
    if s < 60:
        return f'{s} s'
    m, sec = divmod(s, 60)
    if m < 60:
        return f'{m} min {sec:02d}' if sec else f'{m} min'
    h, m = divmod(m, 60)
    return f'{h} h {m:02d}'


def _hhmm(ep):
    try:
        return datetime.fromtimestamp(ep).strftime('%H:%M')
    except (TypeError, ValueError, OSError):
        return ''


GREEN, AMBER, RED, GREY = '#2e7d32', '#b26a00', '#c62828', '#777'
_TD = 'padding:5px 8px;border-bottom:1px solid #eee;font-size:12.5px'
_TH = 'padding:5px 8px;text-align:left;font-size:11.5px;color:#555;border-bottom:2px solid #ddd'


def _tile(label, value, color='#1a1a1a'):
    return (f'<div style="flex:1;min-width:120px;background:#f7f7f8;border-radius:8px;padding:10px 12px">'
            f'<div style="font-size:11px;color:{GREY};text-transform:uppercase;letter-spacing:.03em">{label}</div>'
            f'<div style="font-size:22px;font-weight:700;color:{color};margin-top:2px">{value}</div></div>')


def _part_badge(sc):
    # Le drapeau 'partial' de l'analyseur est posé sur tout scénario suivi d'un autre
    # (donc même en jeu normal entrée->central->scène) : il ne signale PAS une interruption.
    # On ne signale que 'short' (durée bien inférieure à la médiane = probablement passé/écourté).
    if sc is None:
        return f'<span style="color:{GREY}">—</span>'
    if sc['short']:
        return f'<span style="color:{AMBER}">{PART_LABEL[sc["part"]]} ⚠ écourté</span>'
    return f'<span style="color:{GREEN}">{PART_LABEL[sc["part"]]} ✓</span>'


def render_html(data, date_label=''):
    specs = data['spectacles']
    esp = data['esp']
    ch = data['choices']
    h = []

    # ── tuiles ──
    h.append('<div style="display:flex;gap:8px;flex-wrap:wrap;margin:6px 0 16px">')
    h.append(_tile('Spectacles complets', data['n_complete'], GREEN if data['n_complete'] else GREY))
    if data['n_partial']:
        h.append(_tile('Scénarios isolés / partiels', data['n_partial'], AMBER))
    h.append(_tile('Durée totale', _fmt_dur(data['grand_total_s'])))
    inc_color = RED if esp['total_reconnects'] else GREEN
    h.append(_tile('Déconnexions ESP', esp['total_reconnects'], inc_color))
    if ch['total']:
        h.append(_tile('Choix aux boîtiers', ch['total']))
    h.append('</div>')

    # ── spectacles (Forêt) ──
    h.append('<h3 style="font-size:15px;margin:18px 0 6px">Spectacles — Forêt</h3>')
    if not specs:
        h.append(f'<p style="color:{GREY};font-size:13px">Aucun scénario identifié dans les logs ce jour.</p>')
    else:
        h.append(f'<table style="border-collapse:collapse;width:100%"><tr>'
                 f'<th style="{_TH}">Début</th><th style="{_TH}">Univers</th>'
                 f'<th style="{_TH}">Entrée</th><th style="{_TH}">Central</th><th style="{_TH}">Scène</th>'
                 f'<th style="{_TH}">Durée</th><th style="{_TH}">Depuis le précédent</th></tr>')
        shown = specs[:30]
        for sp in shown:
            by_part = {sc['part']: sc for sc in sp['scenarios']}
            gap = f'{_fmt_dur(sp["gap_before"])}' if sp['gap_before'] else '—'
            name_color = GREEN if sp['complete'] else AMBER
            h.append(f'<tr><td style="{_TD}">{_hhmm(sp["start"])}</td>'
                     f'<td style="{_TD};font-weight:600;color:{name_color}">{sp["maison"]}</td>'
                     f'<td style="{_TD}">{_part_badge(by_part.get("ENTREE"))}</td>'
                     f'<td style="{_TD}">{_part_badge(by_part.get("CENTRAL"))}</td>'
                     f'<td style="{_TD}">{_part_badge(by_part.get("SCENE"))}</td>'
                     f'<td style="{_TD}">{_fmt_dur(sp["duration"])}</td>'
                     f'<td style="{_TD};color:{GREY}">{gap}</td></tr>')
        if len(specs) > len(shown):
            h.append(f'<tr><td colspan="7" style="{_TD};color:{GREY}">… et {len(specs) - len(shown)} autre(s)</td></tr>')
        h.append('</table>')

    # ── choix ──
    if ch['total']:
        h.append('<h3 style="font-size:15px;margin:18px 0 6px">Choix aux boîtiers</h3>')
        by_box = ' · '.join(f'{k} : {v}' for k, v in sorted(ch['by_box'].items())) or '—'
        t = ch['times']
        tt = (f'temps du choix moy {round(statistics.mean(t))}s / min {round(min(t))}s / max {round(max(t))}s'
              if t else 'temps non mesuré')
        defpart = ''
        if ch['default']:
            dt = ch['default_times']
            defpart = (f' — <span style="color:{AMBER}">{ch["default"]} par défaut (délai écoulé'
                       + (f', ~{round(statistics.mean(dt))}s' if dt else '') + ')</span>')
        h.append(f'<p style="font-size:13px;margin:2px 0">Par boîtier : {by_box}{defpart}</p>'
                 f'<p style="font-size:12.5px;color:{GREY};margin:2px 0">{tt}</p>')

    # ── ESP : tous les appareils connectés, avec leur latence (déconnexions en couleur) ──
    h.append('<h3 style="font-size:15px;margin:18px 0 6px">ESP — déconnexions et latence</h3>')
    ndrop = sum(1 for d in esp['devices'] if d['reconnects'])
    lat = (f'Latence de connexion (toutes cartes) : moy {esp["lat_avg"]} ms / min {esp["lat_min"]} / max {esp["lat_max"]}'
           if esp['lat_avg'] is not None else '')
    off = ''
    if esp['offline_count']:
        off = (f' · {esp["offline_count"]} passage(s) hors ligne, '
               f'total {_fmt_dur(esp["offline_total_s"])}, plus long {_fmt_dur(esp["offline_longest_s"])}')
    summ = (f'{len(esp["devices"])} ESP connecté(s), '
            + (f'{ndrop} avec déconnexion(s)' if ndrop else 'aucune déconnexion 🎉'))
    h.append(f'<p style="font-size:12.5px;color:{GREY};margin:2px 0">{summ}. {lat}{off}</p>')
    if esp['devices']:
        h.append(f'<table style="border-collapse:collapse;width:100%"><tr>'
                 f'<th style="{_TH}">Appareil</th><th style="{_TH}">Déconnexions</th>'
                 f'<th style="{_TH}">Latence moy</th><th style="{_TH}">min – max</th>'
                 f'<th style="{_TH}">Dernière cause</th></tr>')
        for d in esp['devices'][:40]:
            if d['reconnects']:
                dc = f'<span style="color:{RED};font-weight:600">{d["reconnects"]}</span>'
            else:
                dc = f'<span style="color:{GREEN}">0</span>'
            avg = f'{d["lat_avg"]} ms' if d['lat_avg'] is not None else '—'
            mnmx = f'{d["lat_min"]} – {d["lat_max"]}' if d['lat_avg'] is not None else '—'
            # latence élevée en orange
            if d['lat_avg'] is not None and d['lat_avg'] > 500:
                avg = f'<span style="color:{AMBER}">{avg}</span>'
            h.append(f'<tr><td style="{_TD}">{d["name"]}</td>'
                     f'<td style="{_TD}">{dc}</td>'
                     f'<td style="{_TD}">{avg}</td>'
                     f'<td style="{_TD};color:{GREY}">{mnmx}</td>'
                     f'<td style="{_TD};color:{GREY}">{(d["cause"] or "")[:40]}</td></tr>')
        if len(esp['devices']) > 40:
            h.append(f'<tr><td colspan="5" style="{_TD};color:{GREY}">… et {len(esp["devices"]) - 40} autre(s)</td></tr>')
        h.append('</table>')
    if esp['n_never']:
        names = ', '.join(esp['never'][:8]) + (f' … (+{esp["n_never"] - 8})' if esp['n_never'] > 8 else '')
        h.append(f'<p style="font-size:12px;color:{AMBER};margin:4px 0">'
                 f'{esp["n_never"]} appareil(s) jamais joignable(s) ce jour : {names}</p>')

    # ── autres maisons (résumé BLE) ──
    if data['remote']:
        h.append('<h3 style="font-size:15px;margin:18px 0 6px">Autres maisons '
                 '<span style="font-weight:400;color:#999;font-size:12px">— résumé remonté par BLE</span></h3>')
        h.append(f'<table style="border-collapse:collapse;width:100%"><tr>'
                 f'<th style="{_TH}">Maison</th><th style="{_TH}">Scénarios</th>'
                 f'<th style="{_TH}">Durée</th><th style="{_TH}">Partiels</th><th style="{_TH}">Erreurs</th></tr>')
        for m, r in sorted(data['remote'].items()):
            ec = RED if r['errors'] else GREEN
            pc = AMBER if r['partial'] else GREY
            h.append(f'<tr><td style="{_TD};font-weight:600">{m}</td>'
                     f'<td style="{_TD}">{r["n"]}</td><td style="{_TD}">{_fmt_dur(r["duration"])}</td>'
                     f'<td style="{_TD};color:{pc}">{r["partial"] or ""}</td>'
                     f'<td style="{_TD};color:{ec}">{r["errors"] or ""}</td></tr>')
        h.append('</table>')

    return '\n'.join(h)
