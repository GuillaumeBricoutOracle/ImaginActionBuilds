#!/usr/bin/env python3
"""ImaginAction Wiki — serveur local de documentation du projet + dashboard de supervision."""

import os, re, json, webbrowser, threading, socket as _socket, time as _time
import re
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote
from queue import Queue, Empty, Full

from loganalyzer import LogAnalyzer
from notifier import Mailer, AlertManager, load_config, unity_process_running, hardware_digest
import espcontrol
import otadeploy

# ─── ESP Monitor (in-memory) ──────────────────────────────────────────────────
_esp_registry: dict = {}
_esp_lock = threading.Lock()

_activity_log: list = []
_activity_lock = threading.Lock()

_sse_clients: list = []
_sse_lock = threading.Lock()

def _sse_broadcast(event_type, payload):
    msg = ('event: ' + event_type + '\ndata: '
           + json.dumps(payload, ensure_ascii=False) + '\n\n').encode('utf-8')
    with _sse_lock:
        dead = []
        for q in _sse_clients:
            try:
                q.put_nowait(msg)
            except Full:
                # Client qui ne consomme plus (onglet gelé, PC en veille) : sa
                # file est pleine -> on le considère mort au lieu d'accumuler
                # sans fin en RAM toute la nuit.
                dead.append(q)
            except Exception:
                dead.append(q)
        for q in dead:
            try: _sse_clients.remove(q)
            except Exception: pass

WIKI_DIR     = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.normpath(os.path.join(WIKI_DIR, '..'))
ASSETS_DIR    = os.path.join(PROJECT_ROOT, 'Assets')
if not os.path.isdir(ASSETS_DIR):
    # Build Unity : pas de dossier Assets/, mais un <App>_Data/StreamingAssets
    # a cote de l'exe. On pointe ASSETS_DIR dessus pour que tous les chemins
    # ASSETS_DIR/StreamingAssets/... continuent de fonctionner.
    for _entry in sorted(os.listdir(PROJECT_ROOT)):
        _cand = os.path.join(PROJECT_ROOT, _entry)
        if _entry.endswith('_Data') and os.path.isdir(os.path.join(_cand, 'StreamingAssets')):
            ASSETS_DIR = _cand
            break
WIKI_DOCS_DIR = os.path.join(WIKI_DIR, 'docs')
LIB_DIR       = os.path.join(WIKI_DIR, 'lib')
ICONS_DIR     = os.path.join(ASSETS_DIR, 'StreamingAssets', 'ActionIcons')
PORT          = 5757

# ─── Dashboard : analyse de logs, alertes, mails ─────────────────────────────
# Un fichier par jour : Logs/scenario_console_<AAAA-MM-JJ>.txt, écrits par
# ScenarioFileLogger (Unity). Le dashboard montre exactement les fichiers
# présents dans ce dossier : en déposer ou en retirer un suffit, l'analyseur
# relit tout seul. Un ancien journal unique se découpe par jour avec :
#   python loganalyzer.py --split ancien.txt
SCENARIO_LOG_DIR = os.path.join(PROJECT_ROOT, 'Logs')

_analyzer = LogAnalyzer(SCENARIO_LOG_DIR)


def _log_days():
    """Nombre de fichiers-jours chargés d'office (les plus récents). Les plus
    anciens se chargent quand on consulte leur période. Clé log_days de
    dashboard_config.json ; None = valeur par défaut de l'analyseur."""
    try:
        n = int(load_config().get('log_days') or 0)
    except (TypeError, ValueError):
        n = 0
    return n if n > 0 else None
_mailer   = Mailer()
_alerts   = AlertManager(_mailer, broadcast=lambda ev, pl: _sse_broadcast(ev, pl))

# ─── Sources de logs multiples (autres machines) ─────────────────────────────
# Des dossiers Logs/ d'autres PC (copies, clés USB, partages réseau montés)
# déclarés dans dashboard_config.json ("log_sources"). Un analyseur INDÉPENDANT
# par source : mélanger les lignes de deux machines dans un même parseur
# corromprait les sessions/spectacles. Les alertes et le rapport quotidien
# restent sur la source locale (les copies ne sont pas du temps réel).
_extra_analyzers = {}
_extra_lock = threading.Lock()

def _list_sources():
    sources = [{'src': '', 'name': 'Cette machine', 'path': SCENARIO_LOG_DIR,
                'exists': os.path.isdir(SCENARIO_LOG_DIR)}]
    for entry in load_config().get('log_sources', []):
        name = str(entry.get('name') or '').strip()
        path = str(entry.get('path') or '').strip()
        if not name or not path:
            continue
        sources.append({'src': name, 'name': name,
                        'path': path, 'exists': os.path.exists(path)})
    return sources

def _get_analyzer(src):
    src = (src or '').strip()
    if not src:
        return _analyzer
    with _extra_lock:
        if src in _extra_analyzers:
            return _extra_analyzers[src]
    for entry in load_config().get('log_sources', []):
        if str(entry.get('name') or '').strip() == src and entry.get('path'):
            # path = dossier des fichiers-jours de cette machine (un ancien
            # chemin de fichier est toléré : son dossier est pris).
            ana = LogAnalyzer(os.path.normpath(str(entry['path'])), max_days=_log_days())
            try:
                ana.rebuild()
            except Exception:
                pass
            with _extra_lock:
                _extra_analyzers[src] = ana
            return ana
    return _analyzer

def _ranges_from_query(qs, ana=None):
    """Plages temporelles depuis la query string : ?shows=1,2,3 OU ?from=&to= (ISO)."""
    from datetime import datetime as _dt
    ana = ana or _analyzer
    now = _time.time()
    shows_param = qs.get('shows', [''])[0]
    if shows_param:
        idx = {int(x) for x in shows_param.split(',') if x.strip().isdigit()}
        shows = ana.shows_summary()['shows']
        ranges = [(s['start'], s['end'] if s['end'] else now)
                  for s in shows if s['id'] in idx]
        return ranges or [(now - 1, now)]
    raw_from = qs.get('from', [''])[0]
    raw_to = qs.get('to', [''])[0]
    try:
        t0 = _dt.fromisoformat(raw_from).timestamp() if raw_from else now - 86400
    except ValueError:
        t0 = now - 86400
    try:
        t1 = _dt.fromisoformat(raw_to).timestamp() if raw_to else now
    except ValueError:
        t1 = now
    return [(min(t0, t1), max(t0, t1))]


_hardware_actions_cache = {'ts': 0.0, 'data': {}}

def _get_hardware_actions():
    """Catalogue des actions par type d'appareil, depuis Hardware_Action.csv
    (le même CSV qu'Unity utilise pour construire ses actions).
    -> {device_type: [{action, label, params: [{key,label,type,default,allowed,min,max,description}]}]}
    """
    now = _time.time()
    if now - _hardware_actions_cache['ts'] < 60:
        return _hardware_actions_cache['data']
    import csv as _csv
    result = {}
    try:
        settings_path = os.path.join(ASSETS_DIR, 'StreamingAssets', 'ScenarioSettings.json')
        csv_rel = 'CSV/Hardware_Action.csv'
        if os.path.isfile(settings_path):
            try:
                s = json.load(open(settings_path, encoding='utf-8'))
                csv_rel = s.get('dataSources', {}).get('hardwareActionsCsv', csv_rel)
            except Exception:
                pass
        csv_path = os.path.join(ASSETS_DIR, 'StreamingAssets', csv_rel.replace('/', os.sep))
        if os.path.isfile(csv_path):
            with open(csv_path, encoding='utf-8-sig', newline='') as f:
                for row in _csv.DictReader(f):
                    row = {(k or '').strip(): (v or '').strip() for k, v in row.items()}
                    dtype = row.get('device_type', '')
                    akey = row.get('action_key', '')
                    pkey = row.get('param_key', '')
                    if not dtype or not akey:
                        continue
                    actions = result.setdefault(dtype, [])
                    entry = next((a for a in actions if a['action'] == akey), None)
                    if entry is None:
                        entry = {'action': akey,
                                 'label': row.get('action_label', akey) or akey,
                                 'params': []}
                        actions.append(entry)
                    # 'hardware' est choisi par le dashboard, pas par un champ.
                    if not pkey or pkey == 'hardware':
                        continue
                    allowed = None
                    if row.get('allowed'):
                        try:
                            allowed = json.loads(row['allowed'])
                        except Exception:
                            allowed = None
                    entry['params'].append({
                        'key': pkey,
                        'label': row.get('param_label', pkey) or pkey,
                        'type': row.get('param_type', 'text'),
                        'default': row.get('default', ''),
                        'allowed': allowed,
                        'min': row.get('min', ''),
                        'max': row.get('max', ''),
                        'description': row.get('description', ''),
                    })
    except Exception:
        pass
    _hardware_actions_cache.update(ts=now, data=result)
    return result


_event_names_cache = {'ts': 0.0, 'map': {}, 'files': {}, 'choices': {}}

def _event_auto_label(ev):
    """Libellé lisible d'un event sans nom : sa première action, ou son type."""
    for a in ev.get('actions') or []:
        act = str(a.get('action') or '').strip()
        if not act or act == 'status':
            continue
        o = a.get('options') or {}
        hw = str(o.get('hardware') or '').strip()
        hw = re.sub(r'^[A-Za-z]{3}_', '', hw)          # For_Central_Led_X -> Central_Led_X
        media = str(o.get('filename') or o.get('file') or '').strip()
        media = os.path.splitext(os.path.basename(media))[0] if media else ''
        who = hw or str(o.get('channel') or a.get('type') or '').strip()
        return ' '.join(x for x in (f'{who} ·' if who else '', act, media) if x).strip()
    kind = str(ev.get('event_type') or '')
    role = str(ev.get('event_role') or '')
    if role in ('Start', 'Stop'):
        return {'Start': 'Début', 'Stop': 'Fin'}[role]
    return {'CommentEvent': 'Commentaire', 'InteractionEvent': 'Interaction'}.get(kind, '')


def _get_scenario_events():
    """{'map': {id: nom}, 'files': {fichier: {id: nom}},
        'choices': {fichier: [points de choix]}} depuis les JSON de scénario."""
    now = _time.time()
    if now - _event_names_cache['ts'] < 60:
        return _event_names_cache
    mapping, files, choices = {}, {}, {}
    real = {}     # fichier -> présent hors Test_Media (un vrai scénario, pas une copie de test)
    sa_dir = os.path.join(ASSETS_DIR, 'StreamingAssets')
    if os.path.isdir(sa_dir):
        for dp, _dns, fns in os.walk(sa_dir):
            for fn in fns:
                if not fn.endswith('.json'):
                    continue
                try:
                    d = json.load(open(os.path.join(dp, fn),
                                       encoding='utf-8', errors='replace'))
                except Exception:
                    continue
                if not isinstance(d, dict) or 'events' not in d:
                    continue
                file_map, file_choices = {}, []
                for ev in d.get('events', []):
                    ev_id = ev.get('id')
                    if not ev_id:
                        continue
                    name = ((ev.get('interface') or {}).get('name') or '').strip()
                    if not name or name == ev_id:
                        name = _event_auto_label(ev)
                    file_map[ev_id] = name or ev_id
                    if name:
                        mapping.setdefault(ev_id, name)
                    # Point de choix = event dont une action porte next_event_id
                    overrides = []
                    for a in ev.get('actions', []):
                        o = a.get('options') or {}
                        nid = str(o.get('next_event_id') or '').strip()
                        if not nid:
                            continue
                        sh = str(o.get('sensor_hardware') or '').strip()
                        sv = str(o.get('sensor_variable') or '').strip()
                        expected = str(o.get('expected', o.get('value', '')) or '').strip()
                        cond = f'{sh}.{sv} == {expected}' if (sh or sv) else ''
                        overrides.append({'target': nid, 'condition': cond})
                    if overrides:
                        file_choices.append({
                            'id': ev_id,
                            'timeout_s': ev.get('duration'),
                            'default': str(ev.get('next_event_id') or '').strip() or None,
                            'overrides': overrides,
                        })
                if file_map:
                    files[fn] = file_map
                    rel = os.path.relpath(dp, sa_dir).replace('\\', '/')
                    real[fn] = real.get(fn, False) or not rel.startswith('Test_Media')
                if file_choices:
                    choices[fn] = file_choices
    _event_names_cache.update(ts=now, map=mapping, files=files, choices=choices, real=real)
    return _event_names_cache


def _detect_show_scenarios(shows):
    """Ajoute scenario_detected à chaque spectacle : le fichier de scénario qui
    contient son premier event. Le nom loggué par Unity au START SCENARIO est
    celui du début de la playlist ; après enchaînement, il ment."""
    scen = _get_scenario_events()
    files, real = scen['files'], scen.get('real') or {}

    def rank(fn):
        # Plusieurs versions partagent les ids : un vrai scénario (hors
        # Test_Media) d'abord, puis la version numérotée la plus haute.
        m = re.search(r'_v(\d+)', fn, re.I)
        return (bool(real.get(fn)), int(m.group(1)) if m else -1, fn)

    for s in shows:
        fe = s.get('first_event')
        if not fe:
            continue
        cands = [fn for fn, ids in files.items() if fe in ids]
        if not cands:
            continue
        logged = (s.get('scenario') or '') + '.json'
        pick = logged if logged in cands else max(cands, key=rank)
        s['scenario_detected'] = os.path.splitext(pick)[0]
    return shows


def _avg(vals):
    vals = [v for v in vals if isinstance(v, (int, float))]
    return round(sum(vals) / len(vals), 1) if vals else None

def _get_event_names():
    return _get_scenario_events()['map']


def _enrich_stats(stats):
    """Complète un résultat de stats_range avec les données de scénario :
    noms d'events, events jamais atteints, points de choix (choix vs défaut)."""
    scen = _get_scenario_events()
    names = scen['map']
    for ev in stats.get('events', {}).get('top', []):
        ev['name'] = names.get(ev['id'], '')
    for br in stats.get('events', {}).get('branches', []):
        br['name'] = names.get(br['id'], '')
        for opt in br['options']:
            opt['name'] = names.get(opt['id'], '')

    fired = set(stats.get('events', {}).get('fired_ids') or [])
    if not fired:
        stats['choices'] = []
        return stats

    # Fichier de scénario : on privilégie le nom loggué par Unity, MAIS seulement
    # s'il contient réellement les events joués. Unity peut logguer le scénario de
    # démarrage (ScenarioSettings) alors qu'un autre a été chargé via le pupitre :
    # dans ce cas le nom ment, on retombe sur le recouvrement d'ids.
    def _overlap(fn):
        return len(fired & set(scen['files'].get(fn, {})))

    best_file = None
    for sname in stats.get('scenario_names') or []:
        for fn in scen['files']:
            if os.path.splitext(fn)[0] == sname:
                # nom reconnu seulement s'il recouvre >50% des events joués
                if _overlap(fn) >= 0.5 * len(fired):
                    best_file = fn
                break
        if best_file:
            break
    if best_file is None:
        best_key = (0, 0)
        for fn, ids in scen['files'].items():
            overlap = len(fired & set(ids))
            key = (overlap, -len(ids))
            if key > best_key:
                best_file, best_key = fn, key
    if best_file:
        fmap = scen['files'][best_file]
        unreached = [{'id': i, 'name': fmap[i]} for i in fmap if i not in fired]
        stats['events']['unreached'] = unreached[:30]
        stats['events']['unreached_total'] = len(unreached)
        stats['events']['scenario_guess'] = best_file

    # Points de choix : options déclarées croisées avec l'observé.
    trans = stats.get('transitions') or {}
    choices_out = []
    for cd in (scen['choices'].get(best_file) or []):
        cid = cd['id']
        observed = trans.get(cid, {})
        total = sum(v['count'] for v in observed.values())
        timeout_s = cd.get('timeout_s') or 0
        tol = 2.0
        rows, seen = [], set()
        for ov in cd['overrides']:
            t = ov['target']
            if t in seen:
                for r in rows:
                    if r['target'] == t and r['kind'] == 'choix' \
                            and ov['condition'] and ov['condition'] not in r['condition']:
                        r['condition'] += '  OU  ' + ov['condition']
                continue
            seen.add(t)
            obs = observed.get(t) or {}
            durs = obs.get('durs') or []
            if t == cd.get('default') and timeout_s:
                fast = [x for x in durs if x < timeout_s - tol]
                n_fast = len(fast)
                n_slow = max(0, obs.get('count', 0) - n_fast)
                rows.append({'target': t, 'kind': 'choix',
                             'condition': ov['condition'],
                             'count': n_fast, 'avg_s': _avg(fast)})
                rows.append({'target': t, 'kind': 'defaut',
                             'condition': f'timeout {timeout_s}s',
                             'count': n_slow,
                             'avg_s': timeout_s if n_slow else None})
            else:
                rows.append({'target': t, 'kind': 'choix',
                             'condition': ov['condition'],
                             'count': obs.get('count', 0),
                             'avg_s': _avg(durs)})
        d_id = cd.get('default')
        if d_id and d_id not in seen:
            obs = observed.get(d_id) or {}
            rows.append({'target': d_id, 'kind': 'defaut',
                         'condition': (f'timeout {timeout_s}s'
                                       if timeout_s else 'chemin par défaut'),
                         'count': obs.get('count', 0),
                         'avg_s': _avg(obs.get('durs') or [])})
        for t, obs in observed.items():
            if t not in seen and t != d_id:
                rows.append({'target': t, 'kind': 'autre',
                             'condition': 'interruption / restart',
                             'count': obs['count'],
                             'avg_s': _avg(obs.get('durs') or [])})
        for r in rows:
            r['name'] = names.get(r['target'], r['target'])
            r['pct'] = round(r['count'] / total * 100) if total else 0
        choices_out.append({'id': cid, 'name': names.get(cid, cid),
                            'timeout_s': timeout_s, 'total': total,
                            'rows': rows})
    stats['choices'] = choices_out
    return stats


def _merge_counter(dst, src_dict):
    for k, v in (src_dict or {}).items():
        dst[k] = dst.get(k, 0) + v
    return dst


def _merge_stats(parts):
    """Fusionne les stats de plusieurs machines. parts = [(label, stats), ...].
    Agrégation au niveau des statistiques — jamais des lignes de log brutes."""
    if len(parts) == 1:
        parts[0][1]['sources'] = [parts[0][0]]
        return parts[0][1]

    from collections import Counter as _Counter
    out = {'ranges': parts[0][1].get('ranges'), 'sources': [p[0] for p in parts]}

    # Boucles / spectacles
    recent, durations = [], []
    for label, st in parts:
        for lp in (st.get('loops', {}).get('recent') or []):
            entry = dict(lp)
            entry['src'] = label
            recent.append(entry)
            if lp.get('duration_s') is not None:
                durations.append(lp['duration_s'])
    recent.sort(key=lambda x: x.get('ts') or '')
    import statistics as _stats
    out['loops'] = {
        'count': sum(st.get('loops', {}).get('count', 0) for _l, st in parts),
        'median_s': round(_stats.median(durations), 1) if durations else None,
        'min_s': round(min(durations), 1) if durations else None,
        'max_s': round(max(durations), 1) if durations else None,
        'recent': recent[-150:],
    }

    # ESP : union (collision de nom improbable -> suffixe machine)
    esp = {}
    for label, st in parts:
        for name, e in (st.get('esp') or {}).items():
            key = name if name not in esp else f'{name} ({label})'
            esp[key] = e
    out['esp'] = esp

    # Events
    ev_by_id, fired, unreached, branches, choices = {}, set(), [], [], []
    scen_names, scen_guess = set(), []
    for label, st in parts:
        events = st.get('events') or {}
        fired.update(events.get('fired_ids') or [])
        for u in (events.get('unreached') or []):
            unreached.append(u)
        if events.get('scenario_guess'):
            scen_guess.append(events['scenario_guess'])
        branches.extend(events.get('branches') or [])
        for c in (st.get('choices') or []):
            c = dict(c)
            c['name'] = f"{c.get('name', c.get('id'))} — {label}"
            choices.append(c)
        scen_names.update(st.get('scenario_names') or [])
        for e in (events.get('top') or []):
            cur = ev_by_id.get(e['id'])
            if cur is None:
                ev_by_id[e['id']] = dict(e)
            else:
                n1, n2 = cur.get('count', 0), e.get('count', 0)
                if cur.get('avg_s') is not None and e.get('avg_s') is not None and n1 + n2 > 0:
                    cur['avg_s'] = round((cur['avg_s'] * n1 + e['avg_s'] * n2) / (n1 + n2), 2)
                cur['count'] = n1 + n2
                if e.get('min_s') is not None:
                    cur['min_s'] = min(x for x in (cur.get('min_s'), e['min_s']) if x is not None)
                if e.get('max_s') is not None:
                    cur['max_s'] = max(x for x in (cur.get('max_s'), e['max_s']) if x is not None)
                cur['stdev_s'] = None   # non recomposable sans les données brutes
    out['events'] = {
        'top': sorted(ev_by_id.values(), key=lambda e: -e.get('count', 0))[:40],
        'fired_ids': list(fired),
        'branches': branches,
        'unreached': unreached[:30],
        'unreached_total': sum((p[1].get('events') or {}).get('unreached_total', 0)
                               for p in parts),
        'scenario_guess': ' + '.join(scen_guess),
    }
    out['choices'] = choices
    out['scenario_names'] = sorted(scen_names)
    out['transitions'] = {}

    # Erreurs
    errors, hard = [], []
    err_groups = {}
    counts = {}
    for label, st in parts:
        errors.extend(st.get('errors_recent') or [])
        hard.extend(st.get('hard_errors_recent') or [])
        _merge_counter(counts, st.get('counts'))
        for g in (st.get('errors_top') or []):
            key = (g.get('level'), (g.get('example') or '')[:80])
            cur = err_groups.get(key)
            if cur is None:
                err_groups[key] = dict(g)
            else:
                cur['count'] += g.get('count', 0)
                cur['last_ts'] = max(cur.get('last_ts') or '', g.get('last_ts') or '')
    errors.sort(key=lambda e: e.get('ts') or '')
    hard.sort(key=lambda e: e.get('ts') or '')
    groups = sorted(err_groups.values(), key=lambda g: -g.get('count', 0))
    out['errors_recent'] = errors[-150:]
    out['hard_errors_recent'] = hard[-100:]
    out['errors_top'] = ([g for g in groups if g.get('level') != 'WARN']
                         + [g for g in groups if g.get('level') == 'WARN'])[:16]
    out['counts'] = counts

    # Séries horaires : somme par heure
    for field in ('level_per_hour', 'activity_per_hour'):
        merged_hours = {}
        for _label, st in parts:
            for hour, c in (st.get(field) or {}).items():
                _merge_counter(merged_hours.setdefault(hour, {}), c)
        out[field] = dict(sorted(merged_hours.items()))

    # Audio, capteurs, boutons, actions
    by_channel, top_files, buttons, actions_total = {}, {}, {}, {}
    plays = snd_err = ws_in = 0
    for _label, st in parts:
        snd = st.get('sound') or {}
        plays += snd.get('plays', 0)
        snd_err += snd.get('errors', 0)
        _merge_counter(by_channel, snd.get('by_channel'))
        for f, n in (snd.get('top_files') or []):
            top_files[f] = top_files.get(f, 0) + n
        ws_in += (st.get('ws_in') or {}).get('count', 0)
        _merge_counter(buttons, st.get('buttons'))
        _merge_counter(actions_total, st.get('actions_total'))
    out['sound'] = {'plays': plays, 'by_channel': by_channel, 'errors': snd_err,
                    'top_files': sorted(top_files.items(), key=lambda kv: -kv[1])[:10]}
    out['ws_in'] = {'count': ws_in}
    out['buttons'] = buttons
    out['actions_total'] = dict(sorted(actions_total.items(),
                                       key=lambda kv: -kv[1])[:15])
    return out


def _stats_multi(qs, srcs):
    """Stats fusionnées pour une liste de sources (mode plage uniquement)."""
    parts = []
    for src in srcs:
        ana = _get_analyzer(src)
        ranges = _ranges_from_query(qs, ana)
        try:
            ana.ensure_coverage(min(t0 for t0, _t1 in ranges))
        except Exception:
            pass
        st = ana.stats_range(ranges)
        _enrich_stats(st)
        parts.append((src or 'Cette machine', st))
    return _merge_stats(parts)


def _stats_for_day(date_str, src=''):
    """Stats d'une journée (00:00 → 24:00) + sessions du jour (pour le rapport)."""
    from datetime import datetime as _dt, timedelta as _td
    ana = _get_analyzer(src)
    try:
        d0 = _dt.strptime(date_str, '%Y-%m-%d')
    except ValueError:
        d0 = _dt.now().replace(hour=0, minute=0, second=0, microsecond=0)
        date_str = d0.strftime('%Y-%m-%d')
    try:
        ana.ensure_coverage(d0.timestamp())
    except Exception:
        pass
    stats = ana.stats_range([(d0.timestamp(), (d0 + _td(days=1)).timestamp())])
    _enrich_stats(stats)
    full = ana.summary()
    stats['sessions'] = [s for s in full['sessions']
                         if (s['start'] or '').startswith(date_str)]
    # Passages hors ligne du jour (pendant les spectacles), pour le rapport.
    stats['offline_episodes'] = _alerts.offline_episodes(date_str)
    # Pour la section Matériel du rapport : les appareils que le(s) scénario(s)
    # du jour utilisent (un appareil éteint hors scénario n'intéresse personne)
    # et les noms d'appareils connus (pour reconnaître un nom mal orthographié).
    # Le nom loggué par Unity peut être celui du scénario de DÉMARRAGE alors
    # qu'un autre a tourné (playlist) : on ajoute le scénario reconnu d'après
    # les events joués (scenario_guess, calculé par _enrich_stats).
    scope = set()
    guess = os.path.splitext((stats.get('events') or {}).get('scenario_guess') or '')[0]
    for name in list(stats.get('scenario_names') or []) + ([guess] if guess else []):
        scope |= _scenario_hardware(name)
    stats['scenario_scope'] = sorted(scope)
    known = set(ana.esp.keys())
    try:
        known |= set(_effective_statuses().keys())
    except Exception:
        pass
    stats['known_hardware'] = sorted(known)
    stats['house_tokens'] = _house_tokens()
    return stats


# Matériel du jour (pannes / jamais joignables / noms inconnus), recalculé par
# le worker toutes les 15 s et servi tel quel dans /api/dashboard/summary.
_today_cache = {'date': None, 'materiel': None, 'scenario': ''}


def _dashboard_worker():
    """Parse initial complet puis tail du log + évaluation des alertes."""
    try:
        _analyzer.max_days = _log_days() or _analyzer.max_days
        _analyzer.rebuild()
    except Exception as exc:
        print(f'[dashboard] parse initial impossible : {exc}')
    last_alert_check = 0.0
    while True:
        _time.sleep(1.0)
        try:
            _analyzer.poll()
        except Exception:
            pass
        # Sources supplémentaires : suivies aussi (utile si le fichier distant
        # est un partage réseau encore alimenté par son PC d'origine).
        with _extra_lock:
            extra_list = list(_extra_analyzers.values())
        for extra in extra_list:
            try:
                extra.poll()
            except Exception:
                pass
        now = _time.time()
        if now - last_alert_check >= 15.0:
            last_alert_check = now
            try:
                summary = _analyzer.summary()

                # Seule règle d'alerte : les passages hors ligne, et uniquement
                # PENDANT une lecture de scénario, pour les appareils que CE
                # scénario utilise. Un décrochage qui n'empêche aucun spectacle
                # de se jouer ne compte pas. On suit le statut EFFECTIF (celui
                # que le tableau affiche), pas la valeur brute d'Unity.
                watched = {}
                if summary.get('scenario_running'):
                    scope = _scenario_hardware(summary.get('scenario_current'))
                    if scope:
                        watched = {name: status
                                   for name, status in _effective_statuses().items()
                                   if name in scope}
                _alerts.track_offline(watched, summary.get('scenario_current'))
                # Rapport quotidien : stats de la journée, pas du log entier.
                # Le même calcul alimente la Vue d'ensemble (matériel du jour).
                _today = _time.strftime('%Y-%m-%d')
                today_stats = _stats_for_day(_today)
                _today_cache['date'] = _today
                _today_cache['materiel'] = hardware_digest(today_stats)
                _today_cache['scenario'] = os.path.splitext(
                    (today_stats.get('events') or {}).get('scenario_guess') or '')[0]
                _alerts.maybe_daily_report(
                    today_stats,
                    image_provider=lambda d=_today: _capture_spectacles_png(d, max_age_s=0))
            except Exception as exc:
                print(f'[dashboard] évaluation alertes : {exc}')

IGNORE_DIRS  = {'Library', 'Temp', 'obj', '.git', 'node_modules', 'Packages',
                '.vs', '__pycache__', 'Wiki', 'FBX', 'TEXTURES', 'Materials',
                'Prefabs', 'UISprites', 'Resources', 'Scenes', 'Settings',
                'StreamingAssets', 'UI Toolkit', '_Recovery'}
IGNORE_FILES = {'AssemblyInfo.cs'}

# ─── Parseur C# ──────────────────────────────────────────────────────────────

_RE_DOC        = re.compile(r'/\*\s*\[(\w[\w ]*)\](.*?)\*/', re.DOTALL)
_RE_XML_BLOCK  = re.compile(r'((?:[ \t]*///[^\n]*\n)+)', re.MULTILINE)
_RE_XML_SUM    = re.compile(r'<summary>(.*?)</summary>', re.DOTALL)
_RE_USING      = re.compile(r'^\s*using\s+([\w.]+)\s*;', re.MULTILINE)
_RE_DECL       = re.compile(
    r'(?:(?:public|internal|private|protected)\s+)?'
    r'(?:(?:static|abstract|sealed|partial|readonly|override|virtual|new)\s+)*'
    r'(class|interface|struct|enum)\s+(\w+)'
    r'(?:\s*<[^>]*>)?'
    r'(?:\s*:\s*([^{;/\n]+?))?'
    r'\s*(?:\{|where\b)',
    re.MULTILINE
)
_RE_FIELD_TYPE = re.compile(
    r'(?:public|private|protected|internal)\s+'
    r'(?:(?:static|readonly|new|virtual|abstract|override|sealed)\s+)*'
    r'([A-Z]\w*)(?:<[^>]+>)?\s+(?:_?\w+)\s*[;={]',
    re.MULTILINE
)
_BUILTIN_TYPES = {
    'String','List','Dictionary','HashSet','Queue','Stack','Array',
    'Boolean','Int32','Single','Double','Byte','Char','Object',
    'MonoBehaviour','ScriptableObject','GameObject','Component',
    'Transform','Rigidbody','Camera','Canvas','Renderer','Collider',
    'Gradient','Color','Color32','Vector2','Vector3','Vector2Int',
    'Material','Shader','Texture','Sprite','AudioClip',
    'RectTransform','LineRenderer','Button','Toggle',
    'TMP_Text','TMP_InputField','TextMeshProUGUI',
    'SerializeField','Header','CreateAssetMenu',
    'PointerEventData','PointerInputModule',
}

# ─── Parseur de méthodes C# ───────────────────────────────────────────────────
_RE_METHOD_DECL = re.compile(
    r'^([ \t]*)(?:\[[^\]]*\][ \t]*\n?[ \t]*)*'
    r'(public|private|protected|internal)'
    r'((?:[ \t]+(?:static|virtual|override|abstract|sealed|async|new|partial|extern|unsafe))*)'
    r'[ \t]+([\w][\w<>\[\]?, \t]*?)[ \t]+(\w+)'
    r'[ \t]*(?:<[^>\n]*>)?[ \t]*\(([^)]*)\)',
    re.MULTILINE
)
_EXCLUDE_M_NAMES = frozenset({
    'if','while','for','foreach','switch','catch','lock','typeof','nameof','sizeof'
})
_EXCLUDE_M_TYPES = frozenset({'class','interface','struct','enum','namespace','delegate'})

def _clean(s):
    return re.sub(r'<[^>]*>', '', s).strip()

def _xml_summaries(src):
    """Retourne {end_pos: description} pour chaque bloc /// <summary>."""
    result = {}
    for bm in _RE_XML_BLOCK.finditer(src):
        sm = _RE_XML_SUM.search(bm.group(1))
        if sm:
            cleaned = re.sub(r'[ \t]*///[ \t]?', '', sm.group(1)).strip()
            result[bm.end()] = cleaned
    return result

def _parse_cs(filepath, rel_path):
    try:
        src = open(filepath, encoding='utf-8', errors='replace').read()
    except Exception:
        return []

    # 1. Block comments /* [Name] ... */
    docs = {}
    for m in _RE_DOC.finditer(src):
        raw = re.sub(r'\n[ \t]*\*[ \t]?', '\n', m.group(2)).strip()
        docs[m.group(1).strip()] = raw

    # 2. XML doc comments /// <summary>
    xml = _xml_summaries(src)

    usings      = _RE_USING.findall(src)
    field_types = list({t for t in _RE_FIELD_TYPE.findall(src) if t not in _BUILTIN_TYPES})

    # 3. Class declarations avec positions dans le source
    class_list = list(_RE_DECL.finditer(src))
    if not class_list:
        return []

    out = []
    for i, m in enumerate(class_list):
        kind        = m.group(1)
        name        = m.group(2)
        raw_parents = m.group(3) or ''
        parents     = [_clean(p) for p in raw_parents.split(',') if _clean(p)]
        inherits    = [p for p in parents if not (p.startswith('I') and len(p) > 1 and p[1].isupper())]
        implements  = [p for p in parents if p.startswith('I') and len(p) > 1 and p[1].isupper()]

        description = docs.get(name, '')
        if not description:
            pos = m.start()
            best_end, best_desc = -1, ''
            for end_pos, desc in xml.items():
                gap = src[end_pos:pos].strip()
                if end_pos <= pos and len(gap) < 200 and end_pos > best_end:
                    best_end, best_desc = end_pos, desc
            description = best_desc

        out.append({
            'name':        name,
            'kind':        kind,
            'inherits':    inherits,
            'implements':  implements,
            'field_types': field_types,
            'usings':      usings,
            'description': description,
            'file':        rel_path.replace('\\', '/'),
            'methods':     [],
            '_start':      m.start(),
            '_end':        class_list[i + 1].start() if i + 1 < len(class_list) else len(src),
        })

    # 4. Méthodes : parsées et attribuées à la classe parente par position
    for mm in _RE_METHOD_DECL.finditer(src):
        access   = mm.group(2)
        mods_raw = mm.group(3).strip()
        ret_raw  = re.sub(r'[ \t]+', ' ', mm.group(4)).strip()
        mname    = mm.group(5)
        prm_raw  = re.sub(r'\s+', ' ', mm.group(6)).strip()

        if mname in _EXCLUDE_M_NAMES or ret_raw in _EXCLUDE_M_TYPES:
            continue

        pos = mm.start()
        best_end, best_desc = -1, ''
        for end_pos, desc in xml.items():
            gap = src[end_pos:pos].strip()
            if end_pos <= pos and len(gap) < 200 and end_pos > best_end:
                best_end, best_desc = end_pos, desc

        method = {
            'access': access,
            'mods':   [x for x in mods_raw.split() if x],
            'ret':    ret_raw,
            'name':   mname,
            'params': prm_raw,
            'desc':   best_desc,
        }

        for cls in reversed(out):
            if cls['_start'] <= pos < cls['_end']:
                cls['methods'].append(method)
                break

    # 5. Nettoyage des champs internes de position
    for cls in out:
        del cls['_start']
        del cls['_end']

    return out


# ─── Détection de cycles ──────────────────────────────────────────────────────

def _find_cycles(mod_edges_list):
    graph = {}
    for e in mod_edges_list:
        graph.setdefault(e['from'], []).append(e['to'])

    cycles, seen_keys, visited = [], set(), set()

    def dfs(node, path, in_path):
        for nb in graph.get(node, []):
            if nb in in_path:
                idx   = path.index(nb)
                cycle = path[idx:]
                min_i = min(range(len(cycle)), key=lambda i: cycle[i])
                key   = tuple(cycle[min_i:] + cycle[:min_i])
                if key not in seen_keys:
                    seen_keys.add(key)
                    cycles.append(list(key))
            elif nb not in visited:
                visited.add(nb)
                path.append(nb)
                in_path.add(nb)
                dfs(nb, path, in_path)
                path.pop()
                in_path.discard(nb)

    for node in list(graph.keys()):
        if node not in visited:
            visited.add(node)
            dfs(node, [node], {node})
    return cycles


# ─── Scene scanner ───────────────────────────────────────────────────────────

def _build_guid_map():
    guid_map = {}
    for root, dirs, files in os.walk(ASSETS_DIR):
        dirs[:] = [d for d in dirs if d != '_Recovery']
        for fname in files:
            if not fname.endswith('.meta'):
                continue
            try:
                with open(os.path.join(root, fname), encoding='utf-8', errors='ignore') as mf:
                    content = mf.read()
                m = re.search(r'guid: ([0-9a-f]+)', content)
                if m:
                    asset_abs = os.path.join(root, fname[:-5])
                    rel = os.path.relpath(asset_abs, ASSETS_DIR).replace('\\', '/')
                    guid_map[m.group(1)] = rel
            except Exception:
                pass
    return guid_map


def _scripts_from_asset(rel_path, guid_map, visited, cache):
    """Recursively collect all .cs scripts reachable from a prefab (follows nested prefabs)."""
    if rel_path in visited:
        return set()
    visited.add(rel_path)
    if rel_path not in cache:
        abs_p = os.path.join(ASSETS_DIR, rel_path.replace('/', '\\'))
        try:
            with open(abs_p, encoding='utf-8', errors='ignore') as f:
                cache[rel_path] = f.read()
        except Exception:
            cache[rel_path] = ''
    txt = cache[rel_path]
    scripts = set(guid_map[g] for g in re.findall(r'm_Script: \{fileID: \d+, guid: ([0-9a-f]+)', txt) if g in guid_map)
    for g in re.findall(r'm_SourcePrefab: \{fileID: \d+, guid: ([0-9a-f]+)', txt):
        if g in guid_map:
            scripts |= _scripts_from_asset(guid_map[g], guid_map, visited, cache)
    return scripts


def _scan_scenes():
    guid_map = _build_guid_map()
    cache = {}
    scenes = []
    for root, dirs, files in os.walk(ASSETS_DIR):
        dirs[:] = [d for d in dirs if d != '_Recovery']
        for fname in files:
            if not fname.endswith('.unity'):
                continue
            scene_abs = os.path.join(root, fname)
            rel_path  = os.path.relpath(scene_abs, ASSETS_DIR).replace('\\', '/')
            try:
                with open(scene_abs, encoding='utf-8', errors='ignore') as f:
                    txt = f.read()
                dir_script_guids = re.findall(r'm_Script: \{fileID: \d+, guid: ([0-9a-f]+)', txt)
                pfb_guids        = re.findall(r'm_SourcePrefab: \{fileID: \d+, guid: ([0-9a-f]+)', txt)
                top_prefabs      = sorted(set(guid_map[g] for g in pfb_guids if g in guid_map))
                all_scripts      = set(guid_map[g] for g in dir_script_guids if g in guid_map)
                for pr in top_prefabs:
                    all_scripts |= _scripts_from_asset(pr, guid_map, set(), cache)
                # Collect prefabs including nested ones via the same traversal
                all_prefabs = set()
                for pr in top_prefabs:
                    _collect_prefabs(pr, guid_map, set(), cache, all_prefabs)
                scenes.append({
                    'name': fname.replace('.unity', ''),
                    'path': rel_path,
                    'scripts': sorted(all_scripts),
                    'prefabs': sorted(all_prefabs | set(top_prefabs)),
                    'unresolved_scripts': len(set(g for g in dir_script_guids if g not in guid_map)),
                    'unresolved_prefabs': len(set(g for g in pfb_guids if g not in guid_map)),
                })
            except Exception:
                pass
    return sorted(scenes, key=lambda s: s['name'])


def _collect_prefabs(rel_path, guid_map, visited, cache, result):
    if rel_path in visited:
        return
    visited.add(rel_path)
    result.add(rel_path)
    if rel_path not in cache:
        abs_p = os.path.join(ASSETS_DIR, rel_path.replace('/', '\\'))
        try:
            with open(abs_p, encoding='utf-8', errors='ignore') as f:
                cache[rel_path] = f.read()
        except Exception:
            cache[rel_path] = ''
    for g in re.findall(r'm_SourcePrefab: \{fileID: \d+, guid: ([0-9a-f]+)', cache[rel_path]):
        if g in guid_map:
            _collect_prefabs(guid_map[g], guid_map, visited, cache, result)


# ─── Scanner ─────────────────────────────────────────────────────────────────

def scan():
    modules_dir = os.path.join(ASSETS_DIR, 'Modules')
    modules, all_classes = {}, []

    if os.path.isdir(modules_dir):
        for mod_name in sorted(os.listdir(modules_dir)):
            mod_path = os.path.join(modules_dir, mod_name)
            if not os.path.isdir(mod_path):
                continue
            classes, files = [], []
            for dp, dns, fns in os.walk(mod_path):
                dns[:] = [d for d in dns if d not in IGNORE_DIRS]
                for fn in sorted(fns):
                    if fn.endswith('.meta'):
                        continue
                    fp  = os.path.join(dp, fn)
                    rel = os.path.relpath(fp, ASSETS_DIR).replace('\\', '/')
                    ext = fn.rsplit('.', 1)[-1].lower() if '.' in fn else ''
                    if ext in ('cs', 'md', 'json'):
                        files.append(rel)
                    if ext == 'cs' and fn not in IGNORE_FILES:
                        for c in _parse_cs(fp, rel):
                            c['module'] = mod_name
                            classes.append(c)
                            all_classes.append(c)
            modules[mod_name] = {
                'name':  mod_name,
                'path':  os.path.relpath(mod_path, PROJECT_ROOT).replace('\\', '/'),
                'classes': classes,
                'files':   files,
            }

    # Dépendances
    known  = {c['name'] for c in all_classes}
    mod_of = {c['name']: c.get('module', '') for c in all_classes}
    deps   = []

    # Scan large : référencement token + graphe d'adjacence par fichier pour le BFS.
    _re_tok = re.compile(r'\b([A-Z]\w*)\b')
    referenced = set()
    broad_adj  = {}   # class_name -> set of known class names referenced in the same file
    if os.path.isdir(modules_dir):
        for dp, dns, fns in os.walk(modules_dir):
            dns[:] = [d for d in dns if d not in IGNORE_DIRS]
            for fn in fns:
                if not fn.endswith('.cs') or fn in IGNORE_FILES:
                    continue
                fp = os.path.join(dp, fn)
                try:
                    src = open(fp, encoding='utf-8', errors='replace').read()
                    defined_here = {m.group(2) for m in _RE_DECL.finditer(src)}
                    refs_in_file = set()
                    for m in _re_tok.finditer(src):
                        t = m.group(1)
                        if t in known and t not in defined_here:
                            referenced.add(t)
                            refs_in_file.add(t)
                    for cls_name in defined_here:
                        if cls_name in known:
                            broad_adj.setdefault(cls_name, set()).update(refs_in_file)
                except Exception:
                    pass

    # BFS depuis les entry-points Unity vers toutes les classes atteignables.
    _UNITY_BASES = {'MonoBehaviour', 'ScriptableObject', 'EditorWindow', 'Editor'}
    unity_roots  = {c['name'] for c in all_classes
                    if any(p in _UNITY_BASES for p in c.get('inherits', []))}
    reachable    = set(unity_roots)
    queue        = list(unity_roots)
    while queue:
        node = queue.pop()
        for nb in broad_adj.get(node, set()):
            if nb not in reachable:
                reachable.add(nb)
                queue.append(nb)

    for c in all_classes:
        fm = mod_of.get(c['name'], '')
        for p in c['inherits']:
            p = _clean(p)
            if p in known:
                deps.append({'from': c['name'], 'to': p, 'type': 'inherits',
                             'fromMod': fm, 'toMod': mod_of.get(p, '')})
        for i in c['implements']:
            i = _clean(i)
            if i in known:
                deps.append({'from': c['name'], 'to': i, 'type': 'implements',
                             'fromMod': fm, 'toMod': mod_of.get(i, '')})
        seen = set()
        for t in c.get('field_types', []):
            if t in known and t != c['name'] and t not in seen:
                seen.add(t)
                deps.append({'from': c['name'], 'to': t, 'type': 'uses',
                             'fromMod': fm, 'toMod': mod_of.get(t, '')})

    # Edges inter-modules
    mod_edges = {}
    for d in deps:
        fm, tm = d['fromMod'], d['toMod']
        if fm and tm and fm != tm:
            key = f'{fm}>{tm}'
            if key not in mod_edges:
                mod_edges[key] = {'from': fm, 'to': tm, 'count': 0, 'details': []}
            mod_edges[key]['count'] += 1
            mod_edges[key]['details'].append({'from': d['from'], 'to': d['to'], 'type': d['type']})

    mod_edges_list = list(mod_edges.values())

    # Métriques de santé par module
    out_mods = {}
    in_mods  = {}
    for e in mod_edges_list:
        out_mods.setdefault(e['from'], set()).add(e['to'])
        in_mods.setdefault(e['to'],   set()).add(e['from'])

    for mod in modules.values():
        n   = mod['name']
        cls = mod['classes']
        cnt = max(len(cls), 1)
        ce  = len(out_mods.get(n, set()))   # efferent coupling
        ca  = len(in_mods.get(n, set()))    # afferent coupling
        mod['health'] = {
            'docRate':     round(sum(1 for c in cls if c['description']) / cnt * 100),
            'outDeps':     ce,
            'inDeps':      ca,
            'ce':          ce,
            'ca':          ca,
            'instability': round(ce / (ca + ce), 2) if (ca + ce) > 0 else 0.5,
        }

    # Rang des modules : profondeur maximale dans le graphe de dépendances.
    # rang 0 = stable/noyau (pas de dépendance sortante), rang max = couche application.
    _mod_graph = {}
    for e in mod_edges_list:
        _mod_graph.setdefault(e['from'], set()).add(e['to'])

    def _mod_rank(node, memo, visiting):
        if node in memo:     return memo[node]
        if node in visiting: return 0   # cycle
        visiting.add(node)
        children = _mod_graph.get(node, set())
        r = 0 if not children else 1 + max(_mod_rank(c, memo, visiting) for c in children)
        visiting.discard(node)
        memo[node] = r
        return r

    _rank_memo = {}
    for mname in list(modules.keys()):
        _mod_rank(mname, _rank_memo, set())
    _max_rank = max(_rank_memo.values()) if _rank_memo else 0
    for mod in modules.values():
        r = _rank_memo.get(mod['name'], 0)
        mod['health']['rank']     = r
        mod['health']['visLevel'] = _max_rank - r   # application = 0 (top), noyau = max (bottom)

    cycles = _find_cycles(mod_edges_list)

    # Fichiers de scénario
    # 1. Liste enregistrée dans ScenarioSettings.json
    _sa_dir      = os.path.join(ASSETS_DIR, 'StreamingAssets')
    settings_fp  = os.path.join(_sa_dir, 'ScenarioSettings.json')
    registered   = set()
    if os.path.exists(settings_fp):
        try:
            ss = json.load(open(settings_fp, encoding='utf-8', errors='replace'))
            for rel in ss.get('scenarios', {}).get('files', []):
                registered.add(os.path.normpath(rel).replace('\\', '/'))
        except Exception:
            pass

    # 2. Découverte automatique : tout JSON contenant une clé "events"
    scenario_files = []
    if os.path.isdir(_sa_dir):
        for dp, dns, fns in os.walk(_sa_dir):
            for fn in fns:
                if not fn.endswith('.json') or fn.endswith('.meta'):
                    continue
                fp  = os.path.join(dp, fn)
                rel = os.path.relpath(fp, _sa_dir).replace('\\', '/')
                try:
                    data = json.load(open(fp, encoding='utf-8', errors='replace'))
                    if 'events' not in data:
                        continue
                    api_path = 'StreamingAssets/' + rel
                    scenario_files.append({
                        'name':       fn,
                        'path':       api_path,
                        'registered': rel in registered or
                                      os.path.normpath(rel).replace('\\','/') in registered,
                    })
                except Exception:
                    pass
    scenario_files.sort(key=lambda x: (not x['registered'], x['name'].lower()))

    # TODO / FIXME scan (fichiers .cs dans les modules)
    todos = []
    _re_todo = re.compile(r'//+\s*(TODO|FIXME|HACK|NOTE|XXX)\b[:\s]*(.*)', re.IGNORECASE)
    if os.path.isdir(modules_dir):
        for dp, dns, fns in os.walk(modules_dir):
            dns[:] = [d for d in dns if d not in IGNORE_DIRS]
            for fn in fns:
                if not fn.endswith('.cs') or fn in IGNORE_FILES:
                    continue
                fp  = os.path.join(dp, fn)
                rel = os.path.relpath(fp, ASSETS_DIR).replace('\\', '/')
                try:
                    lines = open(fp, encoding='utf-8', errors='replace').readlines()
                    for i, line in enumerate(lines):
                        m = _re_todo.search(line)
                        if m:
                            todos.append({
                                'file': rel,
                                'line': i + 1,
                                'tag':  m.group(1).upper(),
                                'text': m.group(2).strip(),
                            })
                except Exception:
                    pass

    # Inventaire hardware (depuis les fichiers scénario)
    hw_map = {}
    for sf in scenario_files:
        fp = os.path.join(ASSETS_DIR, sf['path'])
        try:
            with open(fp, encoding='utf-8', errors='replace') as f:
                data = json.load(f)
            for ev in data.get('events', []):
                ev_name = (ev.get('interface') or {}).get('name', '') or ev.get('id', '')[:8]
                for a in ev.get('actions', []):
                    hw = ((a.get('options') or {}).get('hardware') or '').strip()
                    if not hw:
                        continue
                    if hw not in hw_map:
                        hw_map[hw] = []
                    hw_map[hw].append({
                        'scenario': sf['name'],
                        'event':    ev_name,
                        'type':     a.get('type', ''),
                        'action':   a.get('action', ''),
                    })
        except Exception:
            pass
    hardware_inventory = [{'hardware': hw, 'uses': uses}
                          for hw, uses in sorted(hw_map.items())]

    # ─── GUID → classe (pour Prefabs / Assets) ───────────────────────────────
    guid_to_class = {}
    _re_guid_meta = re.compile(r'^guid:\s*(\w+)', re.MULTILINE)
    _SCAN_IGNORE  = {'Library', 'Temp', '.git', '__pycache__', 'Wiki'}
    if os.path.isdir(ASSETS_DIR):
        for dp, dns, fns in os.walk(ASSETS_DIR):
            dns[:] = [d for d in dns if d not in _SCAN_IGNORE]
            for fn in fns:
                if not fn.endswith('.cs') or fn in IGNORE_FILES:
                    continue
                meta_fp = os.path.join(dp, fn + '.meta')
                if not os.path.isfile(meta_fp):
                    continue
                try:
                    gm = _re_guid_meta.search(
                        open(meta_fp, encoding='utf-8', errors='replace').read())
                    if not gm:
                        continue
                    guid = gm.group(1)
                    cm = _RE_DECL.search(
                        open(os.path.join(dp, fn), encoding='utf-8', errors='replace').read())
                    if cm:
                        guid_to_class[guid] = cm.group(2)
                except Exception:
                    pass

    # ─── Prefabs ──────────────────────────────────────────────────────────────
    prefabs = []
    _re_mscript_guid = re.compile(r'm_Script:\s*\{[^}]*guid:\s*(\w+)')
    if os.path.isdir(ASSETS_DIR):
        for dp, dns, fns in os.walk(ASSETS_DIR):
            dns[:] = [d for d in dns if d not in _SCAN_IGNORE]
            for fn in fns:
                if not fn.endswith('.prefab'):
                    continue
                fp = os.path.join(dp, fn)
                try:
                    src = open(fp, encoding='utf-8', errors='replace').read()
                    scripts, seen_g = [], set()
                    for gm in _re_mscript_guid.finditer(src):
                        guid = gm.group(1)
                        if guid in seen_g:
                            continue
                        seen_g.add(guid)
                        cls = guid_to_class.get(guid)
                        if cls:
                            scripts.append(cls)
                    rel = os.path.relpath(fp, PROJECT_ROOT).replace('\\', '/')
                    prefabs.append({
                        'name':    fn[:-7],
                        'path':    rel,
                        'scripts': scripts,
                    })
                except Exception:
                    pass
    prefabs.sort(key=lambda x: x['name'])

    # ─── ScriptableObject assets ──────────────────────────────────────────────
    so_assets = []
    if os.path.isdir(ASSETS_DIR):
        for dp, dns, fns in os.walk(ASSETS_DIR):
            dns[:] = [d for d in dns if d not in _SCAN_IGNORE]
            for fn in fns:
                if not fn.endswith('.asset'):
                    continue
                fp = os.path.join(dp, fn)
                try:
                    src = open(fp, encoding='utf-8', errors='replace').read()
                    gm = _re_mscript_guid.search(src)
                    if not gm:
                        continue
                    cls = guid_to_class.get(gm.group(1))
                    if not cls:
                        continue
                    rel = os.path.relpath(fp, PROJECT_ROOT).replace('\\', '/')
                    so_assets.append({'name': fn[:-6], 'path': rel, 'scriptClass': cls})
                except Exception:
                    pass
    so_assets.sort(key=lambda x: x['name'])

    # ─── Fichiers runtime requis ─────────────────────────────────────────────
    streaming_dir  = os.path.join(ASSETS_DIR, 'StreamingAssets')
    resources_dir  = os.path.join(ASSETS_DIR, 'Resources')

    # Index fichiers existants : relPath-depuis-sa-racine → abs
    def _index_dir(base):
        idx = {}  # basename → set[relPath],  relPath → abs
        if not os.path.isdir(base):
            return idx
        for dp, dns, fns in os.walk(base):
            for fn in fns:
                if fn.endswith('.meta'):
                    continue
                abs_p = os.path.join(dp, fn)
                rel   = os.path.relpath(abs_p, base).replace('\\', '/')
                idx.setdefault(fn, set()).add(rel)
                idx[rel] = abs_p
        return idx

    sa_idx  = _index_dir(streaming_dir)   # StreamingAssets
    res_idx = _index_dir(resources_dir)   # Resources

    # Patterns de détection (ordre : du plus précis au plus générique)
    # Chaque entrée : (regex, type_hint)
    # type_hint : 'streaming' | 'resources' | 'bare'
    _FILE_EXTS = r'(?:json|csv|xml|txt|bytes|png|jpg|jpeg|wav|mp3|ogg|asset|prefab)'
    _path_patterns = [
        # Path.Combine(Application.streamingAssetsPath, "...")
        (re.compile(
            r'Path\.Combine\s*\([^)]*streamingAssetsPath[^)]*,\s*"([^"]+)"',
            re.IGNORECASE), 'streaming'),
        # streamingAssetsPath + "/" + "..." ou + "..."
        (re.compile(
            r'streamingAssetsPath\s*\+\s*["\'/\\\\]*"([^"]+)"',
            re.IGNORECASE), 'streaming'),
        # Application.dataPath + "/StreamingAssets/..."
        (re.compile(
            r'dataPath\s*\+\s*"[/\\]*StreamingAssets[/\\]+([^"]+)"',
            re.IGNORECASE), 'streaming'),
        # Resources.Load / LoadAsync
        (re.compile(
            r'Resources\.Load\w*\s*(?:<[^>]+>)?\s*\(\s*"([^"]+)"'), 'resources'),
        # Bare string literal with known file extension
        (re.compile(
            r'"((?:[A-Za-z0-9_.+\- ]+[/\\])*[A-Za-z0-9_. +\-]+\.(?:' + _FILE_EXTS + r'))"',
            re.IGNORECASE), 'bare'),
    ]

    def _owner_class(decls, pos):
        best = None
        for dm in decls:
            if dm.start() <= pos:
                best = dm.group(2)
        return best

    def _resolve(raw, hint):
        """Return (expected_location_label, exists: bool, abs_path_or_None)."""
        rel = raw.lstrip('/\\').replace('\\', '/')
        if hint == 'streaming':
            abs_p = os.path.join(streaming_dir, rel.replace('/', os.sep))
            return 'StreamingAssets/' + rel, os.path.isfile(abs_p), abs_p
        if hint == 'resources':
            # Resources.Load has no extension — try several
            for ext in ('', '.asset', '.prefab', '.mat', '.wav', '.mp3', '.png', '.txt'):
                abs_p = os.path.join(resources_dir, (rel + ext).replace('/', os.sep))
                if os.path.isfile(abs_p):
                    return 'Resources/' + rel, True, abs_p
            return 'Resources/' + rel, False, None
        # bare: try streaming first (by full rel, then basename)
        basename = rel.split('/')[-1]
        if rel in sa_idx:
            return 'StreamingAssets/' + rel, True, sa_idx[rel]
        if basename in sa_idx:
            rels = sorted(sa_idx[basename])
            return 'StreamingAssets/' + rels[0], True, None
        if rel in res_idx:
            return 'Resources/' + rel, True, res_idx[rel]
        if basename in res_idx:
            rels = sorted(res_idx[basename])
            return 'Resources/' + rels[0], True, None
        # Unknown location but referenced in code
        return raw, False, None

    # key = expected_location → entry
    req_map = {}

    if os.path.isdir(modules_dir):
        for dp, dns, fns in os.walk(modules_dir):
            dns[:] = [d for d in dns if d not in IGNORE_DIRS]
            for fn in fns:
                if not fn.endswith('.cs') or fn in IGNORE_FILES:
                    continue
                fp  = os.path.join(dp, fn)
                rel_fp = os.path.relpath(fp, ASSETS_DIR).replace('\\', '/')
                try:
                    src   = open(fp, encoding='utf-8', errors='replace').read()
                    decls = list(_RE_DECL.finditer(src))
                    seen_in_file = set()
                    for pattern, hint in _path_patterns:
                        for m in pattern.finditer(src):
                            raw = m.group(1)
                            if (raw, hint) in seen_in_file:
                                continue
                            seen_in_file.add((raw, hint))
                            loc, exists, _ = _resolve(raw, hint)
                            owner = (_owner_class(decls, m.start())
                                     or (decls[0].group(2) if decls else fn[:-3]))
                            entry = req_map.setdefault(loc, {
                                'location': loc,
                                'exists':   exists,
                                'hint':     hint,
                                'refs':     [],
                            })
                            entry['exists'] = entry['exists'] or exists
                            # deduplicate refs by (raw, owner)
                            ref_key = (raw, owner)
                            if not any(r['raw'] == raw and r['owner'] == owner
                                       for r in entry['refs']):
                                entry['refs'].append({
                                    'raw':   raw,
                                    'owner': owner,
                                    'file':  rel_fp,
                                    'hint':  hint,
                                })
                except Exception:
                    pass

    # Ajouter les fichiers StreamingAssets qui ne sont pas référencés (orphelins)
    for fn, rels in sa_idx.items():
        if isinstance(rels, set):   # basenames only (not relPath keys)
            for rel in rels:
                loc = 'StreamingAssets/' + rel
                if loc not in req_map:
                    req_map[loc] = {
                        'location': loc,
                        'exists':   True,
                        'hint':     'orphan',
                        'refs':     [],
                    }

    required_files = sorted(
        req_map.values(),
        key=lambda x: (
            0 if (x['refs'] and not x['exists']) else  # manquant prioritaire
            1 if x['refs'] else                         # référencé + existe
            2,                                           # orphelin
            x['location']
        )
    )

    # Docs projet (Wiki/docs/)
    project_docs = []
    if os.path.isdir(WIKI_DOCS_DIR):
        for fn in sorted(os.listdir(WIKI_DOCS_DIR)):
            if fn.endswith('.md') and not fn.endswith('.meta'):
                project_docs.append({'name': fn[:-3], 'path': f'_wiki/{fn}'})

    return {
        'modules':      list(modules.values()),
        'classes':      all_classes,
        'dependencies': deps,
        'moduleEdges':  mod_edges_list,
        'cycles':       cycles,
        'projectDocs':       project_docs,
        'scenarioFiles':     scenario_files,
        'referenced':        list(referenced),
        'reachable':         list(reachable),
        'todos':             todos,
        'hardwareInventory': hardware_inventory,
        'prefabs':           prefabs,
        'soAssets':          so_assets,
        'requiredFiles':     required_files,
    }


# ─── Catalogue ESP (depuis List_Electronic.csv) ──────────────────────────────

def _int_safe(s):
    try:
        return int(str(s).strip())
    except Exception:
        return 0

_electronics_catalog = None
_electronics_catalog_lock = threading.Lock()

# ─── Fraîcheur des statuts ────────────────────────────────────────────────────
# Le statut d'un appareil vient d'Unity. Deux pièges corrigés ici :
#   - Unity fermé -> les statuts se figent et un « en ligne » devient un
#     mensonge. On ne les croit que tant qu'Unity bat (heartbeat).
#   - un appareil dont on n'a jamais eu de nouvelles reste « inconnu » pour
#     toujours ; au bout d'une heure, on tranche : hors ligne.
UNITY_STALE_SECONDS     = 60      # sans heartbeat au-delà, Unity est absent
UNKNOWN_OFFLINE_SECONDS = 3600    # un inconnu d'une heure est déclaré hors ligne

_unity_last_seen = 0.0
_unity_lock = threading.Lock()
_server_started = _time.time()


def _mark_unity_alive():
    """Appelé par le heartbeat ET par chaque push de canal : les deux prouvent
    qu'Unity tourne."""
    global _unity_last_seen
    with _unity_lock:
        _unity_last_seen = _time.time()


def _unity_is_alive(now=None):
    with _unity_lock:
        last = _unity_last_seen
    return (now or _time.time()) - last <= UNITY_STALE_SECONDS


def _effective_status(stored_status, updated_at, now, unity_alive):
    """Statut affiché, dérivé du statut stocké et de son âge."""
    status = stored_status if unity_alive else 'unknown'
    if status == 'unknown':
        # Jamais de nouvelles : l'ancienneté se compte depuis le démarrage du
        # serveur, sinon un parc jamais vu basculerait hors ligne aussitôt.
        age = now - (updated_at or _server_started)
        if age > UNKNOWN_OFFLINE_SECONDS:
            return 'offline'
    return status


_scenario_hw_cache = {}          # nom de scénario -> (mtime, set de noms d'ESP)
_scenario_hw_lock = threading.Lock()


def _scenario_hardware(scenario_name):
    """Noms des ESP utilisés par un scénario (colonne 'hardware' des actions).

    Sert à ne surveiller, pendant une lecture, que les appareils que ce
    scénario sollicite réellement. Relu si le fichier a changé : les scénarios
    sont édités en continu dans Unity.
    """
    if not scenario_name:
        return set()

    path = None
    scen_dir = os.path.join(ASSETS_DIR, 'StreamingAssets', 'Scenarios')
    for dirpath, _, filenames in os.walk(scen_dir):
        for fn in filenames:
            if fn == scenario_name + '.json':
                path = os.path.join(dirpath, fn)
                break
        if path:
            break
    if not path:
        return set()

    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return set()

    with _scenario_hw_lock:
        cached = _scenario_hw_cache.get(scenario_name)
        if cached and cached[0] == mtime:
            return cached[1]

    names = set()
    try:
        data = json.load(open(path, encoding='utf-8', errors='replace'))
        for ev in data.get('events', []) or []:
            for a in ev.get('actions', []) or []:
                raw = ((a.get('options') or {}).get('hardware') or '').strip()
                # Une action peut viser plusieurs ESP d'un coup.
                for part in raw.replace(';', ',').replace('\n', ',').split(','):
                    part = part.strip()
                    if part:
                        names.add(part)
    except Exception as exc:
        print(f'[dashboard] lecture du scénario {scenario_name} : {exc}')
        return set()

    with _scenario_hw_lock:
        _scenario_hw_cache[scenario_name] = (mtime, names)
    return names


# ─── Capture du dashboard pour le rapport ────────────────────────────────────
# Le rapport du jour est une IMAGE de l'onglet Spectacles & Stats, prise par
# un navigateur headless sur ce même serveur (dashboard?day=…&capture=1). Les
# clients mail n'affichent ni SVG ni script : seule une image passe partout.
_BROWSER_CANDIDATES = [
    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
    r'C:\Program Files\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
]
_capture_lock = threading.Lock()


def _find_browser():
    for path in _BROWSER_CANDIDATES:
        if os.path.isfile(path):
            return path
    return None


def _capture_dir():
    import tempfile
    d = os.path.join(tempfile.gettempdir(), 'imaginaction_captures')
    os.makedirs(d, exist_ok=True)
    return d


def _trim_png_bottom(path, margin=24):
    """Rogne le vide sous le contenu : la fenêtre headless est volontairement
    haute pour ne rien couper, le fond uni en dessous n'a rien à faire dans un
    mail. Sans PIL, l'image reste telle quelle."""
    try:
        from PIL import Image, ImageChops
    except Exception:
        return
    try:
        im = Image.open(path).convert('RGB')
        background = Image.new('RGB', im.size, im.getpixel((im.width - 1, im.height - 1)))
        bbox = ImageChops.difference(im, background).getbbox()
        if not bbox:
            return
        bottom = min(im.height, bbox[3] + margin)
        if bottom < im.height - 2:
            im.crop((0, 0, im.width, bottom)).save(path, 'PNG', optimize=True)
    except Exception as exc:
        print(f'[dashboard] capture : recadrage ignoré ({exc})')


def _capture_spectacles_png(date_str, max_age_s=None, width=900, height=1400, scale=2):
    # width étroit + scale 2 : l'image est réduite à ~700 px dans le mail, un
    # rendu 900 px CSS en double densité y reste net et lisible, là où une
    # capture à 1280 px donnait des libellés de 6 px.
    """Octets PNG de l'onglet Spectacles & Stats pour un jour, ou None.

    Cache par jour : un jour passé ne bouge plus (6 h), aujourd'hui est
    rafraîchi toutes les 10 min. max_age_s=0 force une capture neuve.
    """
    import subprocess
    today = _time.strftime('%Y-%m-%d')
    if max_age_s is None:
        max_age_s = 600 if date_str == today else 6 * 3600

    # Les paramètres de rendu font partie du nom : changer la mise en page
    # invalide le cache au lieu de servir une vieille image.
    path = os.path.join(_capture_dir(), f'spectacles_{date_str}_{width}x{scale}.png')
    try:
        if os.path.isfile(path) and _time.time() - os.path.getmtime(path) < max_age_s:
            with open(path, 'rb') as f:
                return f.read()
    except OSError:
        pass

    browser = _find_browser()
    if not browser:
        print('[dashboard] capture impossible : aucun navigateur (Edge/Chrome) trouvé')
        return None

    url = f'http://127.0.0.1:{PORT}/dashboard?day={date_str}&capture=1#spectacles'
    tmp = path + '.tmp.png'
    # Profil dédié : sinon Edge délègue à l'instance déjà ouverte de
    # l'utilisateur et se termine sans rien produire.
    profile = os.path.join(_capture_dir(), 'browser-profile')
    cmd = [browser, '--headless=new', '--disable-gpu', '--hide-scrollbars',
           '--no-first-run', '--no-default-browser-check',
           f'--user-data-dir={profile}', f'--window-size={width},{height}',
           f'--force-device-scale-factor={scale}',
           '--virtual-time-budget=15000', '--run-all-compositor-stages-before-draw',
           f'--screenshot={tmp}', url]

    with _capture_lock:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
            subprocess.run(cmd, capture_output=True, timeout=90)
        except subprocess.TimeoutExpired:
            print('[dashboard] capture : le navigateur headless a dépassé 90 s')
            return None
        except Exception as exc:
            print(f'[dashboard] capture : {exc}')
            return None

        if not os.path.isfile(tmp):
            print('[dashboard] capture : aucun PNG produit par le navigateur')
            return None
        os.replace(tmp, path)
        _trim_png_bottom(path)

    with open(path, 'rb') as f:
        return f.read()


def _effective_statuses():
    """{nom: statut affiché} pour tout le parc, registre + catalogue."""
    now = _time.time()
    alive = _unity_is_alive(now)
    cat = _get_electronics_catalog()

    out = {}
    with _esp_lock:
        for esp in _esp_registry.values():
            out[esp['id']] = _effective_status(
                esp.get('status', 'unknown'), esp.get('updatedAt'), now, alive)
    for c in cat.values():
        out.setdefault(c['name'], _effective_status('unknown', 0, now, alive))
    return out


def _record_dashboard_send(esp_id, action, options, ok):
    """Journalise un envoi fait DEPUIS le dashboard dans le registre de l'ESP.

    Même liste et même format que les pushes d'Unity (historySent) : le détail
    de l'ESP et le rappel « action précédente » de la modale lisent la même
    source, quelle que soit l'origine de l'envoi. Sans ça, le dashboard
    oubliait aussitôt ce qu'il venait d'envoyer.

    Le résultat renseigne aussi le statut : un envoi qui aboutit est la preuve
    directe que l'appareil répond, un échec réseau qu'il ne répond pas — c'est
    la seule source de statut qui ne dépende pas d'Unity.

    Retourne l'entrée mise à jour (à diffuser en SSE hors du lock).
    """
    sent_json = json.dumps({'action': action, 'options': options or {}},
                           ensure_ascii=False)
    now = _time.time()

    with _esp_lock:
        ex = _esp_registry.get(esp_id, {})
        hist = list(ex.get('historySent', []))
        # Historique : seulement ce qui est réellement parti, et pas de doublon
        # consécutif (renvoyer 3 fois la même action noierait la liste).
        if ok and (not hist or hist[-1].get('msg') != sent_json):
            hist.append({'msg': sent_json, 'ts': now})
            hist = hist[-10:]

        entry = {
            'id':              esp_id,
            'status':          'online' if ok else 'offline',
            'lastReceived':    ex.get('lastReceived', ''),
            'lastSent':        sent_json if ok else ex.get('lastSent', ''),
            'scenario':        ex.get('scenario', ''),
            'meta':            ex.get('meta', {}),
            'updatedAt':       now,
            'historyReceived': list(ex.get('historyReceived', [])),
            'historySent':     hist,
        }
        _esp_registry[esp_id] = entry

    return entry


# ─── Filtre maison ────────────────────────────────────────────────────────────
# Unity ne pilote que les appareils dont le nom contient TOUS les jetons de
# actionEditor.hardwareNameRequiredTokens (AppSettings.json), ex. ["FOR"] sur un
# PC Forêt. Le dashboard applique le même filtre : la liste du matériel couvre
# les quatre maisons, et un PC Forêt n'a que faire des 110 appareils du Banquet.
_house_cache = {'mtime': None, 'tokens': []}


def _house_tokens():
    path = os.path.join(ASSETS_DIR, 'StreamingAssets', 'AppSettings.json')
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return []
    if _house_cache['mtime'] == mtime:
        return _house_cache['tokens']
    tokens = []
    try:
        app = json.load(open(path, encoding='utf-8'))
        raw = (app.get('actionEditor') or {}).get('hardwareNameRequiredTokens') or []
        tokens = [str(t).strip() for t in raw if str(t).strip()]
    except Exception:
        pass
    _house_cache.update(mtime=mtime, tokens=tokens)
    return tokens


def _in_house(name):
    """Même règle qu'Unity (MatchesRequiredTokens) : tous les jetons présents,
    sans tenir compte de la casse. Sans jeton configuré, tout passe."""
    if not name:
        return False
    low = str(name).lower()
    return all(t.lower() in low for t in _house_tokens())


def _get_electronics_catalog():
    global _electronics_catalog
    if _electronics_catalog is not None:
        return _electronics_catalog
    with _electronics_catalog_lock:
        if _electronics_catalog is not None:
            return _electronics_catalog
        import csv as _csv
        catalog = {}
        try:
            settings_path = os.path.join(ASSETS_DIR, 'StreamingAssets', 'ScenarioSettings.json')
            csv_rel = 'Test_Media/CSV/List_Electronic.csv'
            if os.path.isfile(settings_path):
                try:
                    s = json.load(open(settings_path, encoding='utf-8'))
                    csv_rel = s.get('dataSources', {}).get('electronicsCsv', csv_rel)
                except Exception:
                    pass
            csv_path = os.path.join(ASSETS_DIR, 'StreamingAssets',
                                    csv_rel.replace('/', os.sep))
            if os.path.isfile(csv_path):
                with open(csv_path, encoding='utf-8-sig', newline='') as f:
                    for row in _csv.DictReader(f):
                        row = {k.strip(): v.strip() for k, v in row.items()}
                        name = row.get('NOM ESP32', '')
                        if not name:
                            continue
                        nb_s = _int_safe(row.get('NB SERVOS', 0))
                        servo_names = [row.get(f'Servo {i}', '').strip()
                                       for i in range(1, nb_s + 1)
                                       if row.get(f'Servo {i}', '').strip()]
                        catalog[name.lower()] = {
                            'name':       name,
                            'mac':        row.get('MAC ADRESS', ''),
                            'ip':         row.get('IP ADDR', ''),
                            'type':       row.get('TYPE', ''),
                            'maison':     row.get('MAISON', ''),
                            'ilot':       row.get('ILOT', ''),
                            'nbLeds':     _int_safe(row.get('NB LEDS', 0)),
                            'nbLedsUsed': _int_safe(row.get('NB LEDS USED', 0)),
                            'nbServos':   nb_s,
                            'servoNames': servo_names,
                        }
        except Exception:
            pass
        _electronics_catalog = catalog
        return catalog


# ─── Serveur HTTP ─────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def _send(self, body_bytes, mime, code=200):
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', len(body_bytes))
        self.send_header('Access-Control-Allow-Origin', '*')
        # Le dashboard évolue souvent : jamais de cache navigateur (sinon une
        # vieille page affiche des états faux, ex. bandeau "scénario en cours").
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body_bytes)

    def _json(self, data, code=200):
        self._send(json.dumps(data, ensure_ascii=False).encode('utf-8'),
                   'application/json; charset=utf-8', code)

    def do_GET(self):
        p  = urlparse(self.path)
        qs = parse_qs(p.query)

        if p.path == '/':
            html = os.path.join(WIKI_DIR, 'index.html')
            try:
                self._send(open(html, encoding='utf-8').read().encode('utf-8'),
                           'text/html; charset=utf-8')
            except FileNotFoundError:
                self._send(b'<h1>index.html manquant</h1>', 'text/html', 404)

        elif p.path == '/dashboard':
            html = os.path.join(WIKI_DIR, 'dashboard.html')
            try:
                self._send(open(html, encoding='utf-8').read().encode('utf-8'),
                           'text/html; charset=utf-8')
            except FileNotFoundError:
                self._send(b'<h1>dashboard.html manquant</h1>', 'text/html', 404)

        elif p.path == '/api/dashboard/summary':
            summary = _analyzer.summary()
            with _alerts.lock:
                summary['alerts'] = list(_alerts.alerts)[-60:]
            summary['mail_last_result'] = _mailer.last_result
            summary['unity_running'] = unity_process_running()
            # Passages hors ligne : nombre et durée cumulée par appareil.
            summary['offline_stats'] = _alerts.offline_stats()
            cfg = load_config()
            summary['email_enabled'] = bool(cfg.get('email_enabled'))
            summary['recipients_count'] = len(cfg.get('recipients', []))
            summary['house_tokens'] = _house_tokens()
            summary['today'] = dict(_today_cache)
            self._json(summary)

        elif p.path == '/api/hardware-actions':
            dtype = qs.get('type', [''])[0].strip()
            data = _get_hardware_actions()
            self._json(data.get(dtype, []) if dtype else data)

        elif p.path == '/api/dashboard/sources':
            self._json(_list_sources())

        elif p.path == '/api/firmware':
            fw_dir = str(load_config().get('firmware_dir') or '')
            entries = otadeploy.list_firmware(fw_dir)
            cat = _get_electronics_catalog()
            with _esp_lock:
                statuses = {k: v.get('status') for k, v in _esp_registry.items()}
            for e in entries:
                c = cat.get(e['esp'].lower())
                e['ip'] = (c or {}).get('ip', '')
                e['type'] = (c or {}).get('type', '')
                e['known'] = c is not None
                e['status'] = statuses.get(e['esp'], 'unknown')
            self._json({'dir': fw_dir,
                        'dir_exists': os.path.isdir(fw_dir),
                        'scenario_running': _analyzer.summary().get('scenario_running', False),
                        'firmwares': entries})

        elif p.path == '/api/dashboard/shows':
            srcs = [s.strip() for s in qs.get('src', [''])[0].split(',')] or ['']
            if len(srcs) == 1:
                data_shows = _get_analyzer(srcs[0]).shows_summary()
                for s in data_shows['shows']:
                    s['src'] = srcs[0] or 'Cette machine'
                _detect_show_scenarios(data_shows['shows'])
                self._json(data_shows)
            else:
                merged_shows, days = [], set()
                for src in srcs:
                    part = _get_analyzer(src).shows_summary()
                    label = src or 'Cette machine'
                    for s in part['shows']:
                        s['src'] = label
                        merged_shows.append(s)
                    days.update(part['days'])
                merged_shows.sort(key=lambda s: s['start'])
                _detect_show_scenarios(merged_shows)
                self._json({'shows': merged_shows, 'days': sorted(days),
                            'multi': True})

        elif p.path == '/api/dashboard/stats':
            srcs = [s.strip() for s in qs.get('src', [''])[0].split(',')] or ['']
            if len(srcs) == 1:
                ana = _get_analyzer(srcs[0])
                ranges = _ranges_from_query(qs, ana)
                # La période demandée pilote le chargement des fichiers-jours
                # plus anciens que la fenêtre par défaut.
                try:
                    ana.ensure_coverage(min(t0 for t0, _t1 in ranges))
                except Exception:
                    pass
                stats = ana.stats_range(ranges)
                _enrich_stats(stats)
                stats['sources'] = [srcs[0] or 'Cette machine']
                self._json(stats)
            else:
                # Multi-machines : agrégation par plage (la sélection de
                # spectacles reste mono-machine, ses ids sont locaux).
                self._json(_stats_multi(qs, srcs))

        elif p.path == '/api/dashboard/report-image':
            # PNG de l'onglet Spectacles & Stats du jour (affiché dans l'onglet
            # Rapports ; le mail reçoit la même image en inline).
            date = qs.get('date', [''])[0] or _time.strftime('%Y-%m-%d')
            png = _capture_spectacles_png(date)
            if png is None:
                self._json({'error': 'capture indisponible'}, 503)
                return
            self._send(png, 'image/png')

        elif p.path == '/api/dashboard/report':
            date = qs.get('date', [''])[0] or _time.strftime('%Y-%m-%d')
            srcs = [s.strip() for s in qs.get('src', [''])[0].split(',')] or ['']
            if len(srcs) == 1:
                stats = _stats_for_day(date, srcs[0])
                label = f'du {date}' + (f' — {srcs[0]}' if srcs[0] else '')
            else:
                parts, sessions = [], []
                for src in srcs:
                    st = _stats_for_day(date, src)
                    label_src = src or 'Cette machine'
                    for sess in st.get('sessions') or []:
                        sess['machine'] = label_src
                        sessions.append(sess)
                    parts.append((label_src, st))
                stats = _merge_stats(parts)
                stats['sessions'] = sorted(sessions, key=lambda s: s.get('start') or '')
                # Le journal hors ligne est local à ce serveur : pas fusionné.
                stats['offline_episodes'] = _alerts.offline_episodes(date)
                label = f"du {date} — {len(srcs)} machines"
            image_url = f'/api/dashboard/report-image?date={date}'
            self._json({'date': date, 'image': image_url,
                        'html': _alerts.build_report_html(stats, label, image_src=image_url)})

        elif p.path == '/api/scan':
            self._json(scan())

        elif p.path == '/api/file':
            rel = unquote(qs.get('path', [''])[0])
            if rel.startswith('_wiki/'):
                fn   = rel[len('_wiki/'):]
                absp = os.path.normpath(os.path.join(WIKI_DOCS_DIR, fn))
                norm = os.path.normpath(WIKI_DOCS_DIR)
            else:
                absp = os.path.normpath(os.path.join(ASSETS_DIR, rel))
                norm = os.path.normpath(ASSETS_DIR)
            if not absp.startswith(norm + os.sep) and absp != norm:
                self._json({'error': 'Acces refuse'}, 403)
                return
            try:
                self._json({'content': open(absp, encoding='utf-8', errors='replace').read(),
                            'path': rel})
            except FileNotFoundError:
                self._json({'error': 'Fichier introuvable'}, 404)

        elif p.path == '/api/search':
            q = unquote(qs.get('q', [''])[0]).strip()
            if len(q) < 2:
                self._json({'results': [], 'total': 0})
                return
            try:
                pat = re.compile(re.escape(q), re.IGNORECASE)
            except re.error:
                self._json({'error': 'Expression invalide'}, 400)
                return
            results = []
            for dp, dns, fns in os.walk(ASSETS_DIR):
                dns[:] = [d for d in dns if d not in IGNORE_DIRS]
                for fn in fns:
                    if fn.endswith('.meta'):
                        continue
                    ext = fn.rsplit('.', 1)[-1].lower() if '.' in fn else ''
                    if ext not in ('cs', 'md', 'json'):
                        continue
                    fp  = os.path.join(dp, fn)
                    rel = os.path.relpath(fp, ASSETS_DIR).replace('\\', '/')
                    try:
                        lines = open(fp, encoding='utf-8', errors='replace').readlines()
                    except Exception:
                        continue
                    hits = [{'line': i+1, 'text': l.rstrip()}
                            for i, l in enumerate(lines) if pat.search(l)]
                    if hits:
                        results.append({'file': rel, 'matches': hits[:15], 'total': len(hits)})
            if os.path.isdir(WIKI_DOCS_DIR):
                for fn in sorted(os.listdir(WIKI_DOCS_DIR)):
                    if not fn.endswith('.md'):
                        continue
                    fp = os.path.join(WIKI_DOCS_DIR, fn)
                    try:
                        lines = open(fp, encoding='utf-8', errors='replace').readlines()
                    except Exception:
                        continue
                    hits = [{'line': i+1, 'text': l.rstrip()}
                            for i, l in enumerate(lines) if pat.search(l)]
                    if hits:
                        results.append({'file': f'_wiki/{fn}', 'matches': hits[:15], 'total': len(hits)})
            self._json({'q': q, 'results': results,
                        'total': sum(r['total'] for r in results)})

        elif p.path == '/api/refs':
            name = unquote(qs.get('name', [''])[0])
            if not name:
                self._json({'error': 'Parametre name manquant'}, 400)
                return
            pattern = re.compile(r'\b' + re.escape(name) + r'\b')
            results = []
            for dp, dns, fns in os.walk(ASSETS_DIR):
                dns[:] = [d for d in dns if d not in IGNORE_DIRS]
                for fn in fns:
                    if not fn.endswith('.cs'):
                        continue
                    fp  = os.path.join(dp, fn)
                    rel = os.path.relpath(fp, ASSETS_DIR).replace('\\', '/')
                    try:
                        lines = open(fp, encoding='utf-8', errors='replace').readlines()
                    except Exception:
                        continue
                    matches = [{'line': i+1, 'text': l.rstrip()}
                               for i, l in enumerate(lines) if pattern.search(l)]
                    if matches:
                        results.append({'file': rel, 'matches': matches})
            self._json({'name': name, 'results': results})

        elif p.path == '/api/esp-templates':
            hardware = qs.get('hardware', [''])[0].strip()
            _sa_dir2 = os.path.join(ASSETS_DIR, 'StreamingAssets')
            templates: dict = {}
            if os.path.isdir(_sa_dir2):
                for dp2, _, fns2 in os.walk(_sa_dir2):
                    for fn2 in fns2:
                        if not fn2.endswith('.json') or fn2.endswith('.meta'):
                            continue
                        try:
                            d2 = json.load(open(os.path.join(dp2, fn2),
                                               encoding='utf-8', errors='replace'))
                            if 'events' not in d2:
                                continue
                            for ev in d2.get('events', []):
                                ev_name = ((ev.get('interface') or {}).get('name', '')
                                           or ev.get('id', '')[:8])
                                for a in ev.get('actions', []):
                                    hw = ((a.get('options') or {}).get('hardware') or '').strip()
                                    if not hw:
                                        continue
                                    if hardware and hw.lower() != hardware.lower():
                                        continue
                                    key2 = hw
                                    if key2 not in templates:
                                        templates[key2] = []
                                    opts = {k: v for k, v in (a.get('options') or {}).items()
                                            if k != 'hardware'}
                                    entry = {
                                        'scenario': fn2,
                                        'event':    ev_name,
                                        'action':   a.get('action', ''),
                                        'options':  opts,
                                    }
                                    if not any(t['action'] == entry['action']
                                               and t['options'] == entry['options']
                                               for t in templates[key2]):
                                        templates[key2].append(entry)
                        except Exception:
                            pass
            self._json(templates.get(hardware, []) if hardware else templates)

        elif p.path == '/api/scenes':
            self._json(_scan_scenes())

        elif p.path == '/api/activity':
            with _activity_lock:
                self._json(list(_activity_log))

        elif p.path == '/api/events':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('X-Accel-Buffering', 'no')
            self.end_headers()
            # Borne la file : un client sain consomme en continu ; une file qui
            # se remplit = client mort, on le lâche (voir _sse_broadcast).
            q = Queue(maxsize=1000)
            with _sse_lock:
                _sse_clients.append(q)
            try:
                while True:
                    try:
                        msg = q.get(timeout=25)
                        self.wfile.write(msg)
                        self.wfile.flush()
                    except Empty:
                        self.wfile.write(b': heartbeat\n\n')
                        self.wfile.flush()
            except Exception:
                pass
            finally:
                with _sse_lock:
                    try: _sse_clients.remove(q)
                    except Exception: pass

        elif p.path == '/api/esp':
            cat = _get_electronics_catalog()
            now = _time.time()
            alive = _unity_is_alive(now)
            with _esp_lock:
                data_out = []
                seen = set()
                for esp in _esp_registry.values():
                    entry = dict(esp)
                    # 'status' = ce qu'on affiche ; 'statusReported' garde la
                    # valeur brute d'Unity, pour le diagnostic.
                    entry['statusReported'] = entry.get('status', 'unknown')
                    entry['status'] = _effective_status(
                        entry['statusReported'], entry.get('updatedAt'), now, alive)
                    entry['unityAlive'] = alive
                    entry['inHouse'] = _in_house(esp['id'])
                    c = cat.get(esp['id'].lower())
                    if c:
                        entry['catalog'] = c
                    data_out.append(entry)
                    seen.add(esp['id'].lower())
                for key, c in cat.items():
                    if key not in seen:
                        data_out.append({
                            'id':              c['name'],
                            'status':          _effective_status('unknown', 0, now, alive),
                            'statusReported':  'unknown',
                            'unityAlive':      alive,
                            'lastReceived':    '',
                            'lastSent':        '',
                            'scenario':        '',
                            'meta':            {},
                            'updatedAt':       0,
                            'historyReceived': [],
                            'historySent':     [],
                            'catalog':         c,
                            'inHouse':         _in_house(c['name']),
                        })
            self._json(data_out)

        elif p.path.startswith('/icons/'):
            fname = p.path[len('/icons/'):]
            if '/' in fname or '\\' in fname or '..' in fname:
                self._json({'error': 'Acces refuse'}, 403)
                return
            absp = os.path.join(ICONS_DIR, fname)
            try:
                with open(absp, 'rb') as f:
                    data = f.read()
                ext  = fname.rsplit('.', 1)[-1].lower() if '.' in fname else ''
                mime = {'png': 'image/png', 'jpg': 'image/jpeg',
                        'jpeg': 'image/jpeg', 'gif': 'image/gif',
                        'svg': 'image/svg+xml'}.get(ext, 'application/octet-stream')
                self._send(data, mime)
            except FileNotFoundError:
                self._json({'error': 'Icone introuvable'}, 404)

        elif p.path.startswith('/lib/'):
            fname = p.path[len('/lib/'):]
            if '/' in fname or '\\' in fname or '..' in fname:
                self._json({'error': 'Acces refuse'}, 403)
                return
            absp = os.path.join(LIB_DIR, fname)
            try:
                with open(absp, 'rb') as f:
                    data = f.read()
                ext  = fname.rsplit('.', 1)[-1].lower() if '.' in fname else ''
                mime = {'js': 'application/javascript; charset=utf-8',
                        'css': 'text/css; charset=utf-8',
                        'map': 'application/json'}.get(ext, 'application/octet-stream')
                self._send(data, mime)
            except FileNotFoundError:
                self._json({'error': 'Fichier introuvable'}, 404)

        else:
            self._json({'error': 'Not found'}, 404)

    def do_POST(self):
        p      = urlparse(self.path)
        length = int(self.headers.get('Content-Length', 0))
        body   = self.rfile.read(length)
        try:
            data = json.loads(body.decode('utf-8'))
        except Exception:
            self._json({'error': 'JSON invalide'}, 400)
            return

        def _resolve_path(rel):
            """Retourne (abspath, norm_base) ou (None, None) si hors base."""
            if rel.startswith('_wiki/'):
                fn   = rel[len('_wiki/'):]
                absp = os.path.normpath(os.path.join(WIKI_DOCS_DIR, fn))
                norm = os.path.normpath(WIKI_DOCS_DIR)
            else:
                absp = os.path.normpath(os.path.join(ASSETS_DIR, rel))
                norm = os.path.normpath(ASSETS_DIR)
            if not absp.startswith(norm + os.sep):
                return None, None
            return absp, norm

        if p.path == '/api/file-write':
            rel     = data.get('path', '')
            content = data.get('content', '')
            if not rel:
                self._json({'error': 'Parametre path manquant'}, 400)
                return
            absp, _ = _resolve_path(rel)
            if absp is None:
                self._json({'error': 'Acces refuse'}, 403)
                return
            os.makedirs(os.path.dirname(absp), exist_ok=True)
            with open(absp, 'w', encoding='utf-8') as f:
                f.write(content)
            self._json({'ok': True, 'path': rel})

        elif p.path == '/api/file-delete':
            rel = data.get('path', '')
            if not rel:
                self._json({'error': 'Parametre path manquant'}, 400)
                return
            absp, _ = _resolve_path(rel)
            if absp is None:
                self._json({'error': 'Acces refuse'}, 403)
                return
            try:
                os.remove(absp)
                meta = absp + '.meta'
                if os.path.exists(meta):
                    os.remove(meta)
            except FileNotFoundError:
                self._json({'error': 'Fichier introuvable'}, 404)
                return
            self._json({'ok': True})

        elif p.path == '/api/unity/heartbeat':
            # Unity signale qu'il tourne : sans ça, le dashboard ne peut pas
            # distinguer un statut à jour d'un statut figé depuis sa fermeture.
            _mark_unity_alive()
            self._json({'ok': True})
            return

        elif p.path == '/api/esp':
            esp_id = data.get('espId') or data.get('id', '')
            if not esp_id:
                self._json({'error': 'espId manquant'}, 400)
                return
            _mark_unity_alive()  # un push de canal prouve aussi qu'Unity est là
            with _esp_lock:
                ex  = _esp_registry.get(esp_id, {})
                now = _time.time()

                new_recv = data.get('lastReceived', ex.get('lastReceived', ''))
                new_sent = data.get('lastSent',     ex.get('lastSent', ''))

                hist_recv = list(ex.get('historyReceived', []))
                recv_changed = bool(new_recv and (not hist_recv or hist_recv[-1].get('msg') != new_recv))
                if recv_changed:
                    hist_recv.append({'msg': new_recv, 'ts': now})
                    hist_recv = hist_recv[-10:]

                hist_sent = list(ex.get('historySent', []))
                sent_changed = bool(new_sent and (not hist_sent or hist_sent[-1].get('msg') != new_sent))
                if sent_changed:
                    hist_sent.append({'msg': new_sent, 'ts': now})
                    hist_sent = hist_sent[-10:]

                entry = {
                    'id':              esp_id,
                    'status':          data.get('status',   ex.get('status', 'unknown')),
                    'lastReceived':    new_recv,
                    'lastSent':        new_sent,
                    'scenario':        data.get('scenario', ex.get('scenario', '')),
                    'meta':            data.get('meta',     ex.get('meta', {})),
                    'updatedAt':       now,
                    'historyReceived': hist_recv,
                    'historySent':     hist_sent,
                }
                _esp_registry[esp_id] = entry

            self._json({'ok': True})

            # Activity log + SSE broadcast (outside esp_lock)
            act = {
                'ts':       now,
                'espId':    esp_id,
                'status':   entry['status'],
                'received': new_recv if recv_changed else '',
                'sent':     new_sent if sent_changed else '',
                'scenario': entry['scenario'],
            }
            with _activity_lock:
                _activity_log.append(act)
                if len(_activity_log) > 500:
                    del _activity_log[:100]

            cat = _get_electronics_catalog()
            broadcast_entry = dict(entry)
            c = cat.get(esp_id.lower())
            if c:
                broadcast_entry['catalog'] = c
            _sse_broadcast('esp-update', broadcast_entry)
            if recv_changed or sent_changed:
                _sse_broadcast('activity', act)
            return

        elif p.path == '/api/offline/reset':
            _alerts.reset_offline_stats()
            self._json({'ok': True})
            return

        elif p.path == '/api/esp/clear':
            with _esp_lock:
                _esp_registry.clear()
            with _activity_lock:
                _activity_log.clear()
            self._json({'ok': True})
            _sse_broadcast('clear', {})
            return

        elif p.path == '/api/esp/send':
            # Cibles : "names": ["For_...", ...] et/ou "name" et/ou "ip".
            # + "action" (obligatoire), "options" {...}, "port" (défaut 80).
            action = (data.get('action') or '').strip()
            if not action:
                self._json({'error': "Champ 'action' manquant"}, 400)
                return
            # Port imposé par l'appelant seulement s'il est fourni : sinon il
            # dépend du protocole de CHAQUE cible (80 en WebSocket, 55443 en
            # Yeelight), un envoi groupé pouvant mélanger ampoules et ESP32.
            forced_port = int(data['port']) if data.get('port') else None
            options = data.get('options') or {}

            cat = _get_electronics_catalog()
            targets = []
            raw_names = list(data.get('names') or [])
            if data.get('name'):
                raw_names.append(data['name'])
            for nm in raw_names:
                c = cat.get(str(nm).lower()) or {}
                ip = c.get('ip') or (data.get('ip') if len(raw_names) == 1 else '')
                targets.append({'name': str(nm), 'ip': (ip or '').strip(),
                                'type': c.get('type', '')})
            if not targets and data.get('ip'):
                targets.append({'name': data['ip'], 'ip': str(data['ip']).strip(),
                                'type': str(data.get('type') or '')})
            if not targets:
                self._json({'error': 'Aucune cible (names/name/ip manquant)'}, 400)
                return

            # Envois en parallèle : un ESP injoignable ne retarde pas les
            # autres. Parallélisme BORNÉ : un reset de masse vise ~90 ESP, et
            # autant de threads simultanés sature la machine pour rien.
            results, res_lock = [], threading.Lock()
            pending, pending_lock = list(targets), threading.Lock()

            def _send_one(target):
                if not target['ip']:
                    return {'name': target['name'], 'ok': False,
                            'detail': 'IP introuvable dans le CSV'}
                ok, detail = espcontrol.send_action(
                    target['ip'], action, options,
                    device_type=target['type'], hardware_name=target['name'],
                    port=forced_port)
                return {'name': target['name'], 'ok': ok, 'detail': detail}

            def _worker():
                while True:
                    with pending_lock:
                        if not pending:
                            return
                        target = pending.pop()
                    try:
                        entry = _send_one(target)
                    except Exception as exc:  # un envoi raté ne tue pas le worker
                        entry = {'name': target['name'], 'ok': False,
                                 'detail': 'Erreur interne : %s' % exc}
                    with res_lock:
                        results.append(entry)

            threads = [threading.Thread(target=_worker, daemon=True)
                       for _ in range(min(24, len(targets)))]
            for th in threads:
                th.start()
            deadline = _time.time() + 30
            for th in threads:
                th.join(timeout=max(0.1, deadline - _time.time()))

            # Mémorise ce qui est VRAIMENT parti, pour le rappel d'actions de
            # la modale et le détail de l'ESP.
            cat_for_sse = _get_electronics_catalog()
            for r in results:
                # "IP introuvable" n'est pas un verdict réseau : on ne touche
                # pas au statut dans ce cas.
                if not r['ok'] and 'IP introuvable' in r.get('detail', ''):
                    continue
                updated = _record_dashboard_send(r['name'], action, options, r['ok'])
                c = cat_for_sse.get(str(r['name']).lower())
                if c:
                    updated = dict(updated, catalog=c)
                _sse_broadcast('esp-update', updated)

            ok_count = sum(1 for r in results if r['ok'])
            act = {'ts': _time.time(), 'espId': 'dashboard', 'status': '',
                   'received': '', 'scenario': '',
                   'sent': f'{action} → {ok_count}/{len(targets)} cible(s) '
                           + json.dumps(options, ensure_ascii=False)[:120]}
            with _activity_lock:
                _activity_log.append(act)
            _sse_broadcast('activity', act)
            self._json({'ok': ok_count == len(targets), 'sent': ok_count,
                        'total': len(targets), 'results': results})
            return

        elif p.path == '/api/firmware/deploy':
            # Déploiement OTA d'UN ESP (l'orchestration par vagues est côté page).
            esp_name = (data.get('esp') or '').strip()
            if not esp_name:
                self._json({'error': "Champ 'esp' manquant"}, 400)
                return

            fw_dir = str(load_config().get('firmware_dir') or '')
            bin_path = os.path.join(fw_dir, esp_name + '.bin')
            if not os.path.isfile(bin_path):
                self._json({'ok': False, 'detail': f'Binaire introuvable : {esp_name}.bin'}, 404)
                return
            c = _get_electronics_catalog().get(esp_name.lower())
            ip = (c or {}).get('ip', '') or (data.get('ip') or '')
            if not ip:
                self._json({'ok': False, 'detail': 'IP introuvable dans le CSV'}, 400)
                return

            ok, detail, seconds = otadeploy.push_firmware(ip, 80, bin_path)
            act = {'ts': _time.time(), 'espId': esp_name, 'status': '',
                   'received': '', 'scenario': '',
                   'sent': f'[OTA] {"OK" if ok else "ECHEC"} — {detail}'}
            with _activity_lock:
                _activity_log.append(act)
            _sse_broadcast('activity', act)
            self._json({'ok': ok, 'detail': detail, 'seconds': seconds,
                        'md5': otadeploy.file_md5(bin_path)})
            return

        elif p.path == '/api/dashboard/alerts/dismiss':
            alert_id = data.get('id')
            ok = _alerts.dismiss(alert_id) if alert_id is not None else False
            self._json({'ok': ok})
            return

        elif p.path == '/api/dashboard/alerts/clear':
            _alerts.clear_all()
            self._json({'ok': True})
            return

        elif p.path == '/api/dashboard/test-email':
            _mailer.send('[ImaginAction] Test du dashboard',
                         '<p>Ceci est un mail de test envoyé depuis ImaginAction '
                         'Supervision. Si tu le reçois, la configuration SMTP est bonne.</p>')
            self._json({'ok': True,
                        'detail': "Mail de test mis en file d'envoi — vérifie "
                                  "mail_last_result dans /api/dashboard/summary."})
            return

        elif p.path == '/api/dashboard/report-now':
            date = (data.get('date') or _time.strftime('%Y-%m-%d')).strip()
            stats = _stats_for_day(date)
            png = _capture_spectacles_png(date, max_age_s=0)   # envoi manuel : image neuve
            _mailer.send(f'[ImaginAction] Rapport du {date}',
                         _alerts.build_report_html(stats, f'du {date}',
                                                   image_src='cid:spectacles' if png else None),
                         images={'spectacles': png} if png else None)
            self._json({'ok': True,
                        'detail': f"Rapport du {date} mis en file d'envoi"
                                  + ('' if png else ' (sans image : capture indisponible)') + '.'})
            return

        else:
            self._json({'error': 'Not found'}, 404)


def _lan_ip():
    """IP locale sur le LAN (sans dépendre du DNS)."""
    try:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        s.connect(('192.168.0.1', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


if __name__ == '__main__':
    import sys
    os.makedirs(WIKI_DOCS_DIR, exist_ok=True)
    threading.Thread(target=_dashboard_worker, daemon=True).start()
    # 0.0.0.0 : accessible depuis les autres machines du réseau local.
    srv = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    url = f'http://localhost:{PORT}'
    print(f'\n  ImaginAction Wiki       ->  {url}')
    print(f'  Dashboard supervision   ->  http://{_lan_ip()}:{PORT}/dashboard')
    print('  Ctrl+C pour arreter\n')
    if '--no-browser' not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url + '/dashboard')).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\nServeur arrete.')
