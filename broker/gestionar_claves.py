#!/usr/bin/env python3
"""Gestion del archivo de claves por identidad (MENSAJERIA_KEYS_FILE, JSON {identidad: clave}).

  gestionar_claves.py ARCHIVO add <identidad>      # crea clave nueva (imprime SOLO la clave)
  gestionar_claves.py ARCHIVO rotate <identidad>   # reemplaza (la anterior deja de valer ya)
  gestionar_claves.py ARCHIVO remove <identidad>
  gestionar_claves.py ARCHIVO list                 # solo nombres, nunca las claves

El broker relee el archivo solo cuando cambia (mtime): no hace falta reiniciarlo.
"""
import json, os, secrets, sys


def cargar(p):
    try:
        with open(p) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def guardar(p, d):
    tmp = p + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f, indent=2, sort_keys=True)
    os.replace(tmp, p)


def main(argv):
    if len(argv) < 3:
        print(__doc__); return 2
    p, op = argv[1], argv[2]
    d = cargar(p)
    if op == "list":
        print("\n".join(sorted(d))); return 0
    if len(argv) < 4:
        print(__doc__); return 2
    ident = argv[3]
    if op == "add" and ident in d:
        print(f"'{ident}' ya existe (usa rotate)", file=sys.stderr); return 1
    if op in ("add", "rotate"):
        d[ident] = secrets.token_urlsafe(32)
        guardar(p, d); print(d[ident]); return 0
    if op == "remove":
        d.pop(ident, None); guardar(p, d); return 0
    print(__doc__); return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
