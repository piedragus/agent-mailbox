# agent-mailbox

[English version](README.en.md) · [Demo de 2 minutos](examples/quickstart.sh)

**Un buzón de mensajes entre agentes de IA que corren en máquinas distintas** (Claude Code, Codex, OpenHands…).
Cada agente tiene una identidad, se manda tareas y respuestas con los demás por una API HTTP mínima, y el sistema
se ocupa de lo aburrido pero crítico: no perder mensajes, no entrar en loops, avisar cuando el otro no contesta y
dejar auditoría de todo.

> English summary: a tiny Flask + SQLite message broker plus a polling daemon that lets headless coding agents
> (Claude Code, Codex, OpenHands) on different machines delegate work to each other. Atomic claim, loop guards,
> rate limiting, human-in-the-loop flag, read receipts and automatic "recipient is not reading" alerts delivered to
> the *sender agent*. ~700 lines of Python, no external services. MIT.

## ¿Para qué sirve?

Tenés dos o más máquinas (o dos o más agentes en una) y querés que se pasen trabajo sin que vos seas el cartero:

- "nodo-a-claude, necesito que crees este repo" → lo recibe el agente que tiene las credenciales.
- "nodo-b, ¿qué versión de X tenés desplegada?" → contesta el que tiene el servidor.
- Un coordinador opcional (un agente más barato/caro) decide a quién asignarle cada cosa mirando qué puede cada uno.

Lo que lo diferencia de "otra cola de mensajes":

| Problema real | Qué hace este proyecto |
|---|---|
| Dos agentes toman el mismo mensaje | Reclamo atómico (`BEGIN IMMEDIATE`) |
| A le contesta a B, B a A, para siempre | Tope de mensajes por hilo; al pasarlo → `esperando_humano` |
| Un agente se contesta a sí mismo | Un mensaje nunca lo puede tomar quien lo mandó |
| El daemon murió con el mensaje "tomado" | Se devuelve a la cola tras un timeout, cuenta como intento |
| Un agente se desboca y gasta de más | Rate limit local por hora, tools permitidas/denegadas, `WORKDIR` fijo |
| Acción irreversible | Marcador `REQUIERE_HUMANO`: el agente frena y espera una persona |
| "No me contestan y no sé por qué" | Recibos de lectura + **alertas automáticas al agente remitente** |

## Demo (salida real de `examples/quickstart.sh`)

```text
1) nodo-a le manda una tarea a nodo-b
2) recibo ANTES de que nodo-b lo lea:
    "estado": "pendiente",
    "leido": false,
3) nodo-b (su daemon) reclama pendientes:
   tomo #1: decime la hora
4) nodo-b responde:
5) recibo DESPUES:
    "estado": "respondido",
    "leido": true,
    "leido_por": "nodo-b",
    "respondido_en": "2026-10-14T11:50:20.574752Z",
```

## Cómo está armado

```
 ┌──────────┐   POST /api/mensajes      ┌────────────┐   GET /pendientes?maquina=B   ┌──────────────┐
 │ agente A │ ────────────────────────▶ │   broker   │ ◀──────────────────────────── │ daemon de B  │
 │          │ ◀──── alertas / recibos ─ │ Flask+SQLite│ ──── mensaje reclamado ─────▶ │ → claude -p… │
 └──────────┘                           └────────────┘ ◀──── POST respuesta ──────── └──────────────┘
```

- **`broker/`** — API Flask + SQLite (ledger: nunca se borran filas, solo cambian de estado). Una sola instancia.
- **`daemon/`** — un proceso por *identidad* (no por máquina física). Cada pasada: rate limit → pide pendientes →
  arma un prompt con el mensaje + las capacidades de todos → invoca al agente headless → postea la respuesta y
  registra la invocación. `daemon_loop.py` lo mantiene corriendo bajo PM2 (o lo que uses).
- **`docs/PROTOCOLO.md`** — el protocolo en detalle: ciclo de vida, reglas duras, formato, rol coordinador.

### Identidad = agente

`de`/`para` son nombres de identidad (`nodo-a-claude`, `nodo-a-codex`, `nodo-b-claude`…). Si una máquina corre
dos agentes, son dos identidades con su propio `.env`. `para: null` = broadcast.

### Ciclo de vida

`pendiente → tomado → respondido`; si falla: reintenta hasta 3 veces → `fallido`.
`requiere_confirmacion` o hilo demasiado largo → `esperando_humano` (ningún daemon lo toma).

### Recibos de lectura y alertas (lo nuevo)

Cada mensaje guarda `tomado_en` (= *leído* por el destinatario), `respondido_en` y `ultimo_error`.

- `GET /api/mensajes/<id>/recibo` — línea de tiempo del mensaje.
- `GET /api/mensajes/enviados?de=<yo>[&solo_sin_leer=1]` — qué mandé y qué quedó sin leer.
- `GET /api/estado` — vista tipo `ps`: por identidad qué tiene tomado/pendiente, última invocación, `sin_leer`.
- **Alertas al remitente**: si nadie lee tu mensaje en `MENSAJERIA_SIN_LEER_MIN` minutos, o si quedó `fallido`,
  el broker postea en *el mismo hilo* un mensaje `de: broker` dirigido a **quien lo mandó** (otro agente, no al
  humano) con qué pasó y el último error. Una alerta por mensaje y tipo, nunca alerta sobre alertas, y no se
  emiten a identidades listadas en `MENSAJERIA_HUMANOS`. Un watchdog interno las revisa cada
  `MENSAJERIA_WATCHDOG_SEG` aunque ningún daemon esté consultando.
- `GET /api/alertas?para=<identidad>[&marcar=1]` — para sesiones interactivas sin daemon (p. ej. un Claude Code
  abierto): con un hook de inicio de sesión que llame a esto, el agente ve las alertas al arrancar.

## Puesta en marcha rápida

```bash
# Broker
cd broker
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp broker.env.example broker.env      # poné una MENSAJERIA_API_KEY larga y aleatoria
set -a; . ./broker.env; set +a
./venv/bin/gunicorn -w 2 -b 127.0.0.1:8765 app:app

# Mandar un mensaje
curl -s -X POST localhost:8765/api/mensajes -H "X-API-Key: $MENSAJERIA_API_KEY" -H 'Content-Type: application/json' \
  -d '{"de":"humano","para":"nodo-b-claude","asunto":"hola","cuerpo":"respondé OK"}'

# Daemon (por identidad)
cd ../daemon
cp daemon.env.example mi-identidad.env   # MAQUINA, AGENTE, BROKER_URL, API_KEY, WORKDIR, tools permitidas…
set -a; . ./mi-identidad.env; set +a
python3 daemon_loop.py
```

Demo sin agentes reales: `bash examples/quickstart.sh`.

Tests: `cd broker && pytest` · `cd daemon && pytest`.

## Modelo de amenazas (qué cubre y qué no)

**Cubre**
- Claves **por identidad** (`MENSAJERIA_KEYS_FILE`, gestión con `broker/gestionar_claves.py`): el broker fija `de`
  según la clave, así que una identidad no puede hacerse pasar por otra. Con `MENSAJERIA_LEGACY_KEY=0` la clave
  compartida deja de valer.
- Mínimo privilegio: cada identidad solo lee su propia cola, publica solo sus propias capacidades, solo ve los hilos
  en los que participa y solo puede marcar como fallidos los mensajes que ella tomó. Humanos y admins ven todo.
- Comparación de claves en tiempo constante, tope de tamaño de cuerpo y rate limit por identidad.
- Los mensajes de otros agentes se inyectan en el prompt **delimitados y marcados como datos**, no como órdenes.

**No cubre**
- No hay TLS propio: usalo sobre una red cifrada (tailnet, WireGuard) o detrás de un proxy TLS.
- El aislamiento de prompts reduce, **no elimina**, la inyección de instrucciones: un agente engañado sigue
  siendo el riesgo principal. Limitá sus herramientas (`MENSAJERIA_ALLOWED_TOOLS`) y el `WORKDIR`.
- Las claves viven en archivos `.env`/JSON: protegelos (0600) y rotalas si dudás.

## Seguridad — leer antes de exponerlo

- Aun con claves por identidad, es un sistema para una red **de confianza** (p. ej. una tailnet). No lo publiques
  a Internet. Atá el broker a una IP privada (`MENSAJERIA_BIND_HOST`), nunca `0.0.0.0`.
- El daemon ejecuta agentes de código. Empezá con herramientas mínimas (`MENSAJERIA_ALLOWED_TOOLS`) y sumá de a
  una. OpenHands en modo headless no tiene dial de permisos: acotá su `WORKDIR` y qué le asignás.
- Los mensajes son *datos de otros agentes*: tratá su contenido como no confiable al armar prompts.
- Las alertas y recibos son informativos; no reemplazan un monitoreo real de los servicios.

## Estado

Software de uso real pero joven, escrito para una red pequeña de 2–4 identidades. Issues y PRs bienvenidos.
Licencia MIT.
