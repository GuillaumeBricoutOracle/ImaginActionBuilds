#!/usr/bin/env python3
"""Client WebSocket minimal (stdlib uniquement) pour envoyer une action à un ESP32.

Connexion à la demande : ouvre ws://ip:port/ws, envoie une frame texte JSON
(le même protocole que le NetworkHardwareDispatcher d'Unity), ferme proprement.
Aucune connexion permanente -> zéro empreinte sur l'ESP hors des envois manuels.

Tout le parc ne parle pas WebSocket : les ampoules (TYPE RGB/MONO) utilisent
l'API Yeelight en TCP. send_action() aiguille vers le bon transport — c'est le
point d'entrée à utiliser, send_ws_action() ne gérant que les ESP32.
"""

import base64
import json
import os
import socket
import struct
import uuid

import yeelightcontrol

WS_DEFAULT_PORT = 80


def _mask_bytes(data, key):
    return bytes(b ^ key[i % 4] for i, b in enumerate(data))


def _frame(opcode, payload):
    """Frame WebSocket masquée (client -> serveur), FIN=1."""
    key = os.urandom(4)
    header = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += struct.pack('>H', n)
    else:
        header.append(0x80 | 127)
        header += struct.pack('>Q', n)
    return bytes(header) + key + _mask_bytes(payload, key)


def _coerce_option(v):
    """Une option qui encode un tableau JSON ("[1,0,0,...]") doit partir comme
    VRAI tableau : le firmware servo fait copyArray(...as<JsonArray>()) et
    échoue en silence sur une string (masque non initialisé -> tous les servos
    bougent). Le reste reste en string (le firmware parse "3000"/"max" très bien)."""
    if isinstance(v, (list, dict)):
        return v
    if isinstance(v, str):
        t = v.strip()
        if len(t) >= 2 and t[0] == '[' and t[-1] == ']':
            try:
                parsed = json.loads(t)
                if isinstance(parsed, list):
                    return parsed
            except Exception:
                pass
    return str(v)


def send_ws_action(ip, port, action, options=None, path='/ws', timeout=3.0):
    """Envoie {"action": ..., "id": ..., "options": {...}} à l'ESP.

    Retourne (ok: bool, detail: str). Ne lève jamais.
    """
    payload = {
        'action': str(action),
        'id': uuid.uuid4().hex,
        'options': {k: _coerce_option(v) for k, v in (options or {}).items()},
    }
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')

    ws_key = base64.b64encode(os.urandom(16)).decode('ascii')
    request = (
        f'GET {path} HTTP/1.1\r\n'
        f'Host: {ip}:{port}\r\n'
        'Upgrade: websocket\r\n'
        'Connection: Upgrade\r\n'
        f'Sec-WebSocket-Key: {ws_key}\r\n'
        'Sec-WebSocket-Version: 13\r\n'
        '\r\n'
    ).encode('ascii')

    try:
        with socket.create_connection((ip, int(port)), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(request)

            # Réponse du handshake (jusqu'à la fin des headers)
            response = b''
            while b'\r\n\r\n' not in response and len(response) < 8192:
                chunk = sock.recv(1024)
                if not chunk:
                    break
                response += chunk

            status_line = response.split(b'\r\n', 1)[0].decode('ascii', errors='replace')
            if ' 101 ' not in status_line + ' ':
                return False, f'Handshake refusé : {status_line or "aucune réponse"}'

            sock.sendall(_frame(0x1, data))          # frame texte
            sock.sendall(_frame(0x8, b'\x03\xe8'))   # close 1000 (normal)
            return True, f'{action} envoyé à {ip}:{port}'
    except socket.timeout:
        return False, f'Timeout ({timeout}s) vers {ip}:{port}'
    except OSError as exc:
        return False, f'Connexion impossible vers {ip}:{port} : {exc}'
    except Exception as exc:  # défense en profondeur : jamais d'exception sortante
        return False, f'Erreur inattendue : {exc}'


def default_port_for(device_type):
    """Port à utiliser quand l'appelant n'en impose pas (le CSV n'en porte pas)."""
    return yeelightcontrol.DEFAULT_PORT if yeelightcontrol.is_bulb(device_type) else WS_DEFAULT_PORT


def send_action(ip, action, options=None, device_type='', hardware_name='',
                port=None, timeout=3.0):
    """Envoie une action à UNE cible, quel que soit son protocole.

    device_type = colonne TYPE de List_Electronic.csv. RGB/MONO -> Yeelight TCP,
    tout le reste -> WebSocket. Retourne (ok: bool, detail: str).
    """
    port = int(port or default_port_for(device_type))

    if yeelightcontrol.is_bulb(device_type):
        return yeelightcontrol.send_yeelight_action(
            ip, port, action, options, device_type=device_type,
            hardware_name=hardware_name, timeout=timeout)

    return send_ws_action(ip, port, action, options, timeout=timeout)


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 3:
        print('usage: python espcontrol.py <ip> <action> [cle=valeur ...] [--type RGB]')
        sys.exit(1)
    args = sys.argv[3:]
    dtype = ''
    if '--type' in args:
        i = args.index('--type')
        dtype = args[i + 1] if i + 1 < len(args) else ''
        args = args[:i] + args[i + 2:]
    opts = dict(kv.split('=', 1) for kv in args)
    ok, detail = send_action(sys.argv[1], sys.argv[2], opts, device_type=dtype)
    print('OK' if ok else 'ECHEC', '-', detail)
