#!/usr/bin/env python3
"""Client Yeelight (TCP, stdlib uniquement) pour le dashboard.

Les ampoules RGB/MONO ne parlent PAS le protocole WebSocket des ESP32 : elles
exposent l'API Yeelight "LAN Control", une socket TCP (port 55443) où chaque
commande est une ligne JSON terminée par \\r\\n. Le dashboard n'avait qu'un
client WebSocket : toute action envoyée à une ampoule partait vers le port 80
et n'arrivait nulle part.

Le mapping action -> commandes reproduit celui d'Unity
(Assets/Modules/ScenarioDispatcher/YeelightActionMapper.cs) : une même action
avec les mêmes options doit produire exactement les mêmes commandes, qu'elle
vienne du scénario ou du dashboard. Les deux fichiers doivent rester alignés.

Différence avec les ESP : l'ampoule RÉPOND ({"id":1,"result":["ok"]} ou
{"id":1,"error":{...}}). On lit la réponse et on la remonte, donc le dashboard
affiche un vrai succès/échec et pas seulement "trame écrite".
"""

import json
import socket
import threading

DEFAULT_PORT = 55443

# Types de la colonne TYPE de List_Electronic.csv pilotés en Yeelight.
BULB_TYPES = ('RGB', 'MONO')

_id_lock = threading.Lock()
_next_id = 0


def is_bulb(device_type):
    return str(device_type or '').strip().upper() in BULB_TYPES


def _next_command_id():
    global _next_id
    with _id_lock:
        _next_id += 1
        if _next_id <= 0:
            _next_id = 1
        return _next_id


def _clamp(value, lo, hi):
    return lo if value < lo else (hi if value > hi else value)


def _opt(options, key):
    """Valeur brute d'une option, en string ; None si absente/vide."""
    if not options or key not in options:
        return None
    v = options[key]
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _get_int(options, key, default, lo, hi):
    """int.TryParse d'Unity : une valeur non entière retombe sur le défaut."""
    raw = _opt(options, key)
    if raw is not None:
        try:
            return _clamp(int(raw), lo, hi)
        except (TypeError, ValueError):
            pass
    return _clamp(default, lo, hi)


def _get_effect(options):
    raw = (_opt(options, 'effect') or '').lower()
    return raw if raw in ('smooth', 'sudden') else 'smooth'


def _get_duration_ms(options, default_ms=500):
    for key in ('duration', 'duration_ms'):
        raw = _opt(options, key)
        if raw is not None:
            try:
                return _clamp(int(raw), 0, 60000)
            except (TypeError, ValueError):
                pass
    return _clamp(default_ms, 0, 60000)


def _get_fade_ms(options, default_ms=1000):
    # fade_time est en SECONDES (float) dans les scénarios, converti en ms.
    raw = _opt(options, 'fade_time')
    if raw is not None:
        try:
            return _clamp(int(round(float(raw) * 1000)), 0, 60000)
        except (TypeError, ValueError):
            pass
    raw = _opt(options, 'duration')
    if raw is not None:
        try:
            return _clamp(int(raw), 0, 60000)
        except (TypeError, ValueError):
            pass
    return _clamp(default_ms, 0, 60000)


# ─── Constructeurs de commandes ───────────────────────────────────────────────

def _command(method, params):
    return json.dumps({'id': _next_command_id(), 'method': method, 'params': params},
                      ensure_ascii=False, separators=(',', ':'))


def _smooth_duration(effect, duration_ms):
    return _clamp(duration_ms, 30, 60000) if effect == 'smooth' else 0


def _set_power(power, effect, duration_ms):
    return _command('set_power', [power, effect, _smooth_duration(effect, duration_ms)])


def _set_bright(brightness, effect, duration_ms):
    return _command('set_bright', [_clamp(brightness, 1, 100), effect,
                                   _smooth_duration(effect, duration_ms)])


def _rgb_int(r, g, b):
    return ((_clamp(r, 0, 255) & 0xFF) << 16) | ((_clamp(g, 0, 255) & 0xFF) << 8) | (_clamp(b, 0, 255) & 0xFF)


def _set_rgb(r, g, b, effect, duration_ms):
    return _command('set_rgb', [_rgb_int(r, g, b), effect, _smooth_duration(effect, duration_ms)])


def _set_hsv(hue, sat, effect, duration_ms):
    return _command('set_hsv', [_clamp(hue, 0, 359), _clamp(sat, 0, 100), effect,
                                _smooth_duration(effect, duration_ms)])


def _flow_rgb_stay(r, g, b, brightness, duration_ms):
    # count=1, action=1 (stay) ; "durée,mode=1(rgb),rgb_int,luminosité"
    expr = '%d,1,%d,%d' % (_clamp(duration_ms, 50, 60000), _rgb_int(r, g, b), _clamp(brightness, 1, 100))
    return _command('start_cf', [1, 1, expr])


def _flow_mono_stay(color_temp, brightness, duration_ms):
    # count=1, action=1 (stay) ; "durée,mode=2(température),K,luminosité"
    expr = '%d,2,%d,%d' % (_clamp(duration_ms, 50, 60000), _clamp(color_temp, 1700, 6500),
                           _clamp(brightness, 1, 100))
    return _command('start_cf', [1, 1, expr])


def _flow_rgb_fade_out(fade_ms):
    # count=1, action=2 (off) ; fondu vers blanc luminosité 1 puis extinction.
    expr = '%d,1,16777215,1' % _clamp(fade_ms, 50, 60000)
    return _command('start_cf', [1, 2, expr])


def _flow_mono_fade_out(fade_ms, color_temp=4100):
    expr = '%d,2,%d,1' % (_clamp(fade_ms, 50, 60000), _clamp(color_temp, 1700, 6500))
    return _command('start_cf', [1, 2, expr])


def _append_optional_brightness(options, commands, effect, duration_ms):
    """turn_on : n'émet un set_bright que si l'option est renseignée (champ vide
    = luminosité de l'ampoule inchangée, comportement historique)."""
    raw = _opt(options, 'brightness')
    if raw is None:
        return
    try:
        commands.append(_set_bright(_clamp(int(raw), 1, 100), effect, duration_ms))
    except (TypeError, ValueError):
        pass


# ─── Mapping action -> commandes ──────────────────────────────────────────────

def build_commands(device_type, action, options=None, hardware_name=''):
    """Retourne (commands: list[str], error: str|None)."""
    options = options or {}
    act = str(action or '').strip().lower()

    dtype = str(device_type or '').strip().upper()
    if not dtype:
        name = str(hardware_name or '').upper()
        if '_RGB_' in name:
            dtype = 'RGB'
        elif '_MONO_' in name:
            dtype = 'MONO'

    effect = _get_effect(options)
    duration_ms = _get_duration_ms(options, 500)
    commands = []

    if dtype == 'RGB':
        if act == 'turn_on':
            commands.append(_set_power('on', effect, duration_ms))
            _append_optional_brightness(options, commands, effect, duration_ms)
        elif act in ('turn_off', 'reset'):
            # reset : l'API Yeelight n'a pas de "retour à l'état initial", et
            # l'équivalent utile pour une ampoule est l'extinction.
            commands.append(_set_power('off', effect, duration_ms))
        elif act == 'set_color':
            commands.append(_set_rgb(_get_int(options, 'red', 0, 0, 255),
                                     _get_int(options, 'green', 0, 0, 255),
                                     _get_int(options, 'blue', 0, 0, 255),
                                     effect, duration_ms))
        elif act == 'set_brightness':
            commands.append(_set_bright(_get_int(options, 'brightness', 100, 1, 100),
                                        effect, duration_ms))
        elif act in ('fade', 'fade_turn_on_rgb'):
            commands.append(_set_power('on', 'sudden', 0))
            commands.append(_flow_rgb_stay(_get_int(options, 'red', 0, 0, 255),
                                           _get_int(options, 'green', 0, 0, 255),
                                           _get_int(options, 'blue', 0, 0, 255),
                                           _get_int(options, 'brightness', 100, 1, 100),
                                           _get_fade_ms(options, 1000)))
        elif act == 'fade_bg':
            commands.append(_set_bright(_get_int(options, 'brightness', 100, 0, 100),
                                        'smooth', _get_fade_ms(options, 1000)))
        elif act == 'fade_turn_on':
            # Pas de température sur une RGB : approximation en blanc.
            commands.append(_set_power('on', 'sudden', 0))
            commands.append(_flow_rgb_stay(255, 255, 255,
                                           _get_int(options, 'brightness', 100, 1, 100),
                                           _get_fade_ms(options, 1000)))
        elif act == 'fade_turn_off':
            commands.append(_flow_rgb_fade_out(_get_fade_ms(options, 1000)))
        elif act == 'set_hsv':
            commands.append(_set_hsv(_get_int(options, 'hue', 0, 0, 359),
                                     _get_int(options, 'sat', 100, 0, 100),
                                     effect, duration_ms))
        else:
            return [], "Action RGB non supportée : '%s'" % act

    elif dtype == 'MONO':
        if act == 'turn_on':
            commands.append(_set_power('on', effect, duration_ms))
            _append_optional_brightness(options, commands, effect, duration_ms)
        elif act in ('turn_off', 'reset'):
            # reset : voir le commentaire côté RGB — extinction.
            commands.append(_set_power('off', effect, duration_ms))
        elif act == 'set_brightness':
            commands.append(_set_bright(_get_int(options, 'brightness', 100, 1, 100),
                                        effect, duration_ms))
        elif act == 'fade_bg':
            commands.append(_set_bright(_get_int(options, 'brightness', 100, 0, 100),
                                        'smooth', _get_fade_ms(options, 1000)))
        elif act == 'fade_turn_on':
            commands.append(_set_power('on', 'sudden', 0))
            commands.append(_flow_mono_stay(4100,
                                            _get_int(options, 'brightness', 100, 1, 100),
                                            _get_fade_ms(options, 1000)))
        elif act == 'fade_turn_off':
            commands.append(_flow_mono_fade_out(_get_fade_ms(options, 1000)))
        else:
            return [], "Action MONO non supportée : '%s'" % act

    else:
        return [], "Type Yeelight non supporté : '%s'" % (dtype or '(vide)')

    return commands, None


# ─── Envoi ────────────────────────────────────────────────────────────────────

def send_yeelight_action(ip, port, action, options=None, device_type='',
                         hardware_name='', timeout=3.0):
    """Envoie une action à une ampoule. Retourne (ok: bool, detail: str).

    Ne lève jamais. Ouvre la socket à la demande et la referme : aucune
    connexion permanente (les ampoules n'acceptent que quelques clients).
    """
    commands, error = build_commands(device_type, action, options, hardware_name)
    if error:
        return False, error

    port = int(port or DEFAULT_PORT)

    try:
        with socket.create_connection((ip, port), timeout=timeout) as sock:
            sock.settimeout(timeout)

            for cmd in commands:
                sock.sendall((cmd + '\r\n').encode('utf-8'))

            ok, detail = _read_outcome(sock, len(commands), timeout)
            if ok:
                return True, '%s envoyé à %s:%d (%d commande(s)) — %s' % (
                    action, ip, port, len(commands), detail)
            return False, 'Ampoule %s:%d a refusé « %s » : %s' % (ip, port, action, detail)

    except socket.timeout:
        return False, 'Timeout (%.1fs) vers %s:%d' % (timeout, ip, port)
    except OSError as exc:
        # Connexion refusée = "Contrôle LAN" désactivé dans l'app Yeelight,
        # c'est la cause la plus fréquente.
        return False, ("Connexion impossible vers %s:%d : %s "
                       "(vérifier que le « Contrôle LAN » est activé sur l'ampoule)"
                       % (ip, port, exc))
    except Exception as exc:  # défense en profondeur : jamais d'exception sortante
        return False, 'Erreur inattendue : %s' % exc


def _read_outcome(sock, expected, timeout):
    """Lit les réponses de l'ampoule. Retourne (ok, detail).

    L'ampoule répond une ligne JSON par commande. On s'arrête à la première
    erreur, ou quand toutes les réponses sont arrivées. Pas de réponse n'est
    PAS un échec : certaines commandes (start_cf) peuvent rester muettes.
    """
    buffer = b''
    answered = 0
    results = []

    try:
        while answered < expected:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buffer += chunk

            while b'\r\n' in buffer:
                line, buffer = buffer.split(b'\r\n', 1)
                line = line.strip()
                if not line:
                    continue

                try:
                    msg = json.loads(line.decode('utf-8', errors='replace'))
                except ValueError:
                    continue

                # Les notifications spontanées (method "props") ne comptent pas.
                if 'id' not in msg:
                    continue

                answered += 1
                if 'error' in msg:
                    err = msg.get('error') or {}
                    return False, str(err.get('message') or err)
                results.append(','.join(str(x) for x in (msg.get('result') or ['ok'])))
    except socket.timeout:
        pass
    except OSError:
        pass

    if answered == 0:
        return True, 'aucune réponse (commande acceptée sans accusé)'
    return True, ' / '.join(results)


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 4:
        print('usage: python yeelightcontrol.py <ip> <RGB|MONO> <action> [cle=valeur ...]')
        sys.exit(1)
    opts = dict(kv.split('=', 1) for kv in sys.argv[4:])
    ok, detail = send_yeelight_action(sys.argv[1], DEFAULT_PORT, sys.argv[3],
                                      opts, device_type=sys.argv[2])
    print('OK' if ok else 'ECHEC', '-', detail)
