#!/usr/bin/env python3
"""
Wrapper para correr el daemon bajo PM2 (12/sep) en vez de systemd timer.

El comportamiento POR PASADA es exactamente el mismo que antes -- una
corrida acotada de daemon.main() (rate limit local, reclamo de pendientes,
invocacion del agente, posteo de respuesta). Lo unico que cambia es QUIEN
dispara cada pasada: antes un systemd timer externo creaba un proceso nuevo
cada N minutos; ahora es un sleep interno dentro de un proceso que PM2
mantiene vivo -- se reinicia solo si se cae, y arranca solo si rebotea la
maquina (una vez que el usuario `mensajeria` tiene `pm2 startup` + `pm2 save`
configurado).

Una excepcion en una pasada NO tira abajo el proceso -- se loguea y se
sigue con la proxima pasada. Si PM2 lo reinicia igual por otro motivo, eso
esta bien: es la resiliencia que se busca.
"""

import os
import time
import traceback

import daemon

INTERVALO_SEG = int(os.environ.get("MENSAJERIA_INTERVALO_SEG", "300"))


def main():
    print(f"[mensajeria] {daemon.MAQUINA} arrancando (rol={daemon.ROL}, agente={daemon.AGENTE}, "
          f"intervalo={INTERVALO_SEG}s)")
    while True:
        try:
            daemon.main()
        except Exception:
            traceback.print_exc()
        time.sleep(INTERVALO_SEG)


if __name__ == "__main__":
    main()
