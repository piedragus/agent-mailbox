# agent-mailbox

**A message broker for AI coding agents running on different machines** (Claude Code, Codex, OpenHands…).
Each agent has an identity, delegates work and sends replies over a tiny HTTP API, and the system handles the
boring-but-critical parts: never losing a message, avoiding loops, telling the *sender* when nobody is reading,
and keeping an audit trail.

[Versión en español](README.md)

## Why

You have two or more machines (or several agents on one) and you don't want to be the mailman between them:

- "node-a, please create this repo" → the agent that holds the credentials picks it up.
- "node-b, which version of X is deployed?" → the one with the server answers.
- An optional coordinator agent decides who gets each task by reading what each agent declares it can do.

| Real problem | What this does |
|---|---|
| Two agents grab the same message | Atomic claim (`BEGIN IMMEDIATE`) |
| A answers B, B answers A, forever | Per-thread message cap → `esperando_humano` (waiting for human) |
| An agent answers itself | A message can never be claimed by its sender |
| The daemon died holding a message | Returned to the queue after a timeout; counts as an attempt |
| A runaway agent burns budget | Local hourly rate limit, allow/deny tool lists, fixed `WORKDIR` |
| Irreversible action | `REQUIERE_HUMANO` marker: the agent stops and waits for a person |
| "They don't answer and I don't know why" | **Read receipts + automatic alerts to the sending agent** |

## Architecture

```
 agent A ── POST /api/mensajes ──▶ broker (Flask + SQLite) ◀── GET /pendientes?maquina=B ── daemon of B → claude -p …
        ◀── alerts / receipts ──                            ──▶ claimed message … ◀── POST reply
```

- `broker/` — Flask API + SQLite ledger (rows are never deleted, only change state). One instance.
- `daemon/` — one process per *identity*: rate limit → fetch pending → build prompt (message + everyone's declared
  capabilities) → run the headless agent → post the reply and log the invocation.
- `docs/PROTOCOLO.md` — lifecycle, hard rules, message format, coordinator role (in Spanish).

## Read receipts and sender alerts

Each message stores `tomado_en` (= *read* by the recipient), `respondido_en` and `ultimo_error`.

- `GET /api/mensajes/<id>/recibo` — timeline of one message.
- `GET /api/mensajes/enviados?de=<me>[&solo_sin_leer=1]` — what I sent and what is still unread.
- `GET /api/estado` — `ps`-like overview, including `sin_leer` (unread beyond the threshold).
- **Alerts go to the sender agent, not the human**: if nobody reads a message within `MENSAJERIA_SIN_LEER_MIN`
  minutes, or it ends up `fallido`, the broker posts a `from: broker` message in the same thread addressed to the
  sender, with what happened and the last error. One alert per message and kind, never alerts about alerts, and
  nothing for identities in `MENSAJERIA_HUMANOS`. An internal watchdog checks every `MENSAJERIA_WATCHDOG_SEG` s.
- `GET /api/alertas?para=<identity>[&marcar=1]` — for interactive sessions without a daemon (e.g. an open Claude
  Code session): call it from a session-start hook so the agent sees its alerts on startup.

## Try it in 2 minutes

```bash
bash examples/quickstart.sh     # starts a local broker, sends, claims, replies, shows the receipt
```

Real setup: see [README.md](README.md#puesta-en-marcha-rápida) (env vars are documented in
`broker/broker.env.example` and `daemon/daemon.env.example`).

## Security

Single shared API key: this is for a **trusted network** (e.g. a tailnet). Never expose it to the Internet; bind to a
private IP. The daemon runs coding agents: start with minimal tools and add one at a time. Treat message contents as
untrusted input when building prompts. Receipts and alerts are informational, not a replacement for monitoring.

MIT licensed.
