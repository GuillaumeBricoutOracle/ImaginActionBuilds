#!/usr/bin/env python3
"""Déploiement OTA de masse vers les ESP32 (stdlib uniquement).

Pousse les binaires nominatifs produits par compilesketchs.py
(output-sketchs/ota/<NomEsp>.bin) sur l'endpoint /update qu'expose déjà
chaque ESP du parc (AsyncElegantOTA, intégré dans AliceWebSocketServer).

Contrat AsyncElegantOTA (lu dans la source vendorée du repo Esp32) :
  POST /update  multipart/form-data
    - champ 'MD5' = md5 hex du binaire (doit précéder le fichier)
    - fichier (nom de champ libre, filename != "filesystem" => flash appli)
  Réponse 200 "OK" puis reboot de l'ESP ; 400/500 texte en cas d'échec.
"""

import hashlib
import http.client
import os
import time

# Cache des MD5 : (path) -> (size, mtime, md5)
_md5_cache = {}


def file_md5(path):
    try:
        stat = os.stat(path)
    except OSError:
        return None
    cached = _md5_cache.get(path)
    if cached and cached[0] == stat.st_size and cached[1] == stat.st_mtime:
        return cached[2]
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    digest = h.hexdigest()
    _md5_cache[path] = (stat.st_size, stat.st_mtime, digest)
    return digest


def list_firmware(fw_dir):
    """Liste les binaires du dossier OTA : nom d'ESP = nom du fichier."""
    entries = []
    if not fw_dir or not os.path.isdir(fw_dir):
        return entries
    for fn in sorted(os.listdir(fw_dir)):
        if not fn.lower().endswith('.bin'):
            continue
        path = os.path.join(fw_dir, fn)
        try:
            stat = os.stat(path)
        except OSError:
            continue
        entries.append({
            'esp': os.path.splitext(fn)[0],
            'file': fn,
            'size': stat.st_size,
            'mtime': stat.st_mtime,
            'md5': file_md5(path),
        })
    return entries


def push_firmware(ip, port, bin_path, timeout=180):
    """Pousse un binaire sur http://ip:port/update. Retourne (ok, detail, secondes)."""
    started = time.time()
    md5 = file_md5(bin_path)
    if md5 is None:
        return False, f'Binaire introuvable : {bin_path}', 0.0

    try:
        with open(bin_path, 'rb') as f:
            payload = f.read()
    except OSError as exc:
        return False, f'Lecture du binaire impossible : {exc}', 0.0

    boundary = '----AliceOtaBoundary%d' % int(time.time() * 1000)
    filename = os.path.basename(bin_path)
    parts = []
    # Le champ MD5 doit arriver AVANT le fichier (AsyncElegantOTA le lit au 1er chunk)
    parts.append(f'--{boundary}\r\n'
                 'Content-Disposition: form-data; name="MD5"\r\n\r\n'
                 f'{md5}\r\n'.encode('ascii'))
    parts.append(f'--{boundary}\r\n'
                 f'Content-Disposition: form-data; name="firmware"; filename="{filename}"\r\n'
                 'Content-Type: application/octet-stream\r\n\r\n'.encode('ascii'))
    parts.append(payload)
    parts.append(f'\r\n--{boundary}--\r\n'.encode('ascii'))
    body = b''.join(parts)

    try:
        conn = http.client.HTTPConnection(ip, int(port), timeout=timeout)
        conn.request('POST', '/update', body=body, headers={
            'Content-Type': f'multipart/form-data; boundary={boundary}',
            'Content-Length': str(len(body)),
            'Connection': 'close',
        })
        resp = conn.getresponse()
        text = resp.read(500).decode('utf-8', errors='replace').strip()
        conn.close()
        elapsed = round(time.time() - started, 1)
        if resp.status == 200 and 'OK' in text:
            return True, f'Flash OK en {elapsed}s — redémarrage en cours', elapsed
        return False, f'HTTP {resp.status} : {text or "réponse vide"}', elapsed
    except Exception as exc:
        elapsed = round(time.time() - started, 1)
        return False, f'{type(exc).__name__}: {exc}', elapsed


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 3:
        print('usage: python otadeploy.py <ip> <chemin.bin>')
        sys.exit(1)
    ok, detail, secs = push_firmware(sys.argv[1], 80, sys.argv[2])
    print(('OK' if ok else 'ECHEC') + ' - ' + detail)
