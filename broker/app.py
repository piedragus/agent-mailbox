"""
Broker de mensajeria entre agentes (Claude Code, Codex, OpenHands, ...) de distintas maquinas.

Vive en una maquina siempre encendida. SQLite como ledger de mensajes: nunca se
borran filas, solo cambian de estado. Auth por API key compartida (header X-API-Key).

Guardrails que vive ACA (server-side, no dependen del daemon que lo consume):
  - Un mensaje nunca lo puede tomar la misma maquina que lo mando (anti-loop
    trivial: A no se contesta a si misma).
  - Tope de mensajes por hilo (MAX_MSGS_POR_HILO): al superarlo, el hilo pasa
    a 'esperando_humano' y deja de poder tomarse en automatico. Corta loops
    A<->B que se contestan indefinidamente.
  - requiere_confirmacion=1 nunca aparece en /pendientes: ese mensaje solo se
    resuelve a mano (o via notificacion a un humano, fuera del scope de este
    broker).
  - Reclamo atomico de mensajes pendientes via BEGIN IMMEDIATE, evita que dos daemons tomen el
    mismo mensaje en una carrera.
"""

import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Flask, g, jsonify, request

DB_PATH = os.environ.get("MENSAJERIA_DB_PATH", "/var/lib/mensajeria/mensajeria.db")
API_KEY = os.environ.get("MENSAJERIA_API_KEY")
MAX_MSGS_POR_HILO = int(os.environ.get("MENSAJERIA_MAX_MSGS_POR_HILO", "10"))
# Minutos que un mensaje puede quedar en 'tomado' antes de considerar que el
# daemon que lo reclamo murio y devolverlo a la cola (idea tomada de OpenRig:
# continuidad tras caida/reinicio). Tiene que ser mayor al timeout de una
# invocacion headless.
SIN_LEER_MIN = int(os.environ.get("MENSAJERIA_SIN_LEER_MIN", "10"))
TOMADO_TIMEOUT_MIN = int(os.environ.get("MENSAJERIA_TOMADO_TIMEOUT_MIN", "30"))

app = Flask(__name__)

_db_inicializada = False


def get_db():
    global _db_inicializada
    if "db" not in g:
        if not _db_inicializada:
            # Corre el schema (CREATE TABLE IF NOT EXISTS, idempotente) la
            # primera vez que una request real toca la DB, sin importar que
            # servidor WSGI la esta sirviendo. Antes esto solo pasaba bajo
            # `if __name__ == "__main__"`, que gunicorn nunca ejecuta (importa
            # el modulo, no lo corre como main) -- el broker levantaba,
            # /api/health daba bien, pero cualquier endpoint real tiraba 500
            # "no such table" (encontrado en vivo en el deploy de nodo-a,
            # 14/sep). No se llama a nivel de modulo porque eso rompe el
            # import en un entorno sin permiso de escritura sobre el
            # DB_PATH default (ej. corriendo los tests como otro usuario).
            init_db()
            _db_inicializada = True
            iniciar_watchdog()
        g.db = sqlite3.connect(DB_PATH, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    conn = sqlite3.connect(DB_PATH)
    with open(schema_path) as f:
        conn.executescript(f.read())
    # Recibos de lectura (migracion idempotente: ALTER no tiene IF NOT EXISTS).
    cols = {r[1] for r in conn.execute("PRAGMA table_info(mensajes)")}
    for col in ("tomado_en", "respondido_en", "ultimo_error", "alerta_leer_en", "alerta_fallo_en"):
        if col not in cols:
            conn.execute(f"ALTER TABLE mensajes ADD COLUMN {col} TEXT")
    conn.commit()
    conn.close()


def require_api_key(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not API_KEY:
            return jsonify(error="MENSAJERIA_API_KEY no configurada en el servidor"), 500
        if request.headers.get("X-API-Key") != API_KEY:
            return jsonify(error="API key invalida o ausente"), 401
        return fn(*args, **kwargs)

    return wrapper


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def recuperar_tomados_vencidos(db):
    """Devuelve a la cola los mensajes 'tomado' hace mas de TOMADO_TIMEOUT_MIN.

    Sin esto, un daemon que muere (reinicio, OOM, corte de red) despues de
    reclamar un mensaje lo deja trabado en 'tomado' para siempre. Cuenta como
    un intento; al tercero pasa a 'fallido', igual que /fallar. Se llama dentro
    de la transaccion de /pendientes (ya tiene BEGIN IMMEDIATE).
    """
    limite = (datetime.now(timezone.utc) - timedelta(minutes=TOMADO_TIMEOUT_MIN)
              ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    db.execute(
        "UPDATE mensajes SET intentos = intentos + 1, tomado_por = NULL, "
        "estado = CASE WHEN intentos + 1 >= 3 THEN 'fallido' ELSE 'pendiente' END, "
        "ultimo_error = 'timeout: el daemon tomo el mensaje y no contesto', "
        "actualizado_en = ? WHERE estado = 'tomado' AND actualizado_en < ?",
        (now_iso(), limite),
    )


ALERTA_DE = "broker"
# Identidades humanas (no reciben alertas por aca) + el propio broker (no alerta sobre sus alertas).
HUMANOS = tuple(x for x in os.environ.get("MENSAJERIA_HUMANOS", "humano").split(",") if x)
SIN_ALERTA = (ALERTA_DE,) + HUMANOS


def emitir_alertas(db):
    """Avisa AL REMITENTE (un agente, no al humano) cuando su mensaje no llego o fallo.

    - sin leer: nadie lo tomo en SIN_LEER_MIN minutos (destinatario caido / sin sesion).
    - fallo: el mensaje quedo 'fallido' (se agotaron los intentos); incluye ultimo_error.
    Una sola alerta de cada tipo por mensaje. Se postea en el mismo hilo, de='broker'.
    Se llama dentro de la transaccion de /pendientes.
    """
    limite = (datetime.now(timezone.utc) - timedelta(minutes=SIN_LEER_MIN)
              ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    pares = [
        ("alerta_leer_en",
         "SELECT * FROM mensajes WHERE estado = 'pendiente' AND tomado_en IS NULL "
         "AND para IS NOT NULL AND creado_en < ? AND alerta_leer_en IS NULL", (limite,),
         "ALERTA: {para} no leyo tu mensaje #{id} (hace {min}+ min)",
         "Tu mensaje #{id} ({asunto}) para {para} sigue sin leerse. Probablemente {para} esta caido o sin sesion "
         "activa. Revisa su estado (GET /api/estado, GET /api/mensajes/{id}/recibo) y, si tenes acceso, "
         "revisa su daemon por SSH (por SSH, si tenes acceso). Si no podes resolverlo, recien ahi escala al humano."),
        ("alerta_fallo_en",
         "SELECT * FROM mensajes WHERE estado = 'fallido' AND alerta_fallo_en IS NULL", (),
         "ALERTA: tu mensaje #{id} a {para} FALLO",
         "Tu mensaje #{id} ({asunto}) a {para} fallo tras {intentos} intentos. Ultimo error: {ultimo_error}. "
         "Revisa el daemon de {para} (login de Claude Code vencido, servicio caido, etc.) antes de reenviar."),
    ]
    for col, sql, args, asunto_t, cuerpo_t in pares:
        for r in db.execute(sql, args).fetchall():
            if r["de"] in SIN_ALERTA:
                db.execute(f"UPDATE mensajes SET {col} = ? WHERE id = ?", (now_iso(), r["id"]))
                continue
            datos = dict(id=r["id"], para=r["para"] or "todos", asunto=r["asunto"] or "(sin asunto)",
                         intentos=r["intentos"], ultimo_error=r["ultimo_error"] or "(sin detalle)",
                         min=SIN_LEER_MIN)
            db.execute(
                "INSERT INTO mensajes (hilo_id, de, para, asunto, cuerpo, estado, requiere_confirmacion, "
                "responde_a_id, creado_en, actualizado_en) VALUES (?, ?, ?, ?, ?, 'pendiente', 0, NULL, ?, ?)",
                (r["hilo_id"], ALERTA_DE, r["de"], asunto_t.format(**datos), cuerpo_t.format(**datos),
                 now_iso(), now_iso()))
            db.execute(f"UPDATE mensajes SET {col} = ? WHERE id = ?", (now_iso(), r["id"]))


WATCHDOG_SEG = int(os.environ.get("MENSAJERIA_WATCHDOG_SEG", "60"))
_watchdog_iniciado = False


def _watchdog_loop():
    """Revisa alertas cada WATCHDOG_SEG aunque ningun daemon este polleando (0 = desactivado)."""
    import threading  # noqa: F401
    import time
    while True:
        time.sleep(WATCHDOG_SEG)
        try:
            conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            try:
                recuperar_tomados_vencidos(conn)
                emitir_alertas(conn)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()
        except Exception:
            pass  # el proximo ciclo reintenta; no tirar el worker


def iniciar_watchdog():
    global _watchdog_iniciado
    if _watchdog_iniciado or WATCHDOG_SEG <= 0 or app.config.get("TESTING"):
        return
    import threading
    _watchdog_iniciado = True
    threading.Thread(target=_watchdog_loop, daemon=True, name="watchdog-alertas").start()


def row_to_dict(row):
    return {k: row[k] for k in row.keys()}


# ---------------------------------------------------------------------------
# Mensajes
# ---------------------------------------------------------------------------

@app.route("/api/mensajes", methods=["POST"])
@require_api_key
def crear_mensaje():
    data = request.get_json(force=True, silent=True) or {}
    de = data.get("de")
    cuerpo = data.get("cuerpo")
    if not de or not cuerpo:
        return jsonify(error="'de' y 'cuerpo' son obligatorios"), 400

    para = data.get("para")  # None = broadcast
    asunto = data.get("asunto")
    requiere_confirmacion = 1 if data.get("requiere_confirmacion") else 0
    hilo_id = data.get("hilo_id") or str(uuid.uuid4())
    responde_a_id = data.get("responde_a_id")

    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        cur = db.execute(
            "SELECT COUNT(*) AS n FROM mensajes WHERE hilo_id = ?", (hilo_id,)
        )
        cantidad_en_hilo = cur.fetchone()["n"]

        # Guardrail anti-loop: si el hilo ya se paso de vueltas, se corta el
        # automatismo y pasa a revision humana pase lo que pase.
        if cantidad_en_hilo >= MAX_MSGS_POR_HILO:
            requiere_confirmacion = 1

        estado = "esperando_humano" if requiere_confirmacion else "pendiente"

        cur = db.execute(
            """
            INSERT INTO mensajes
                (hilo_id, de, para, asunto, cuerpo, estado, requiere_confirmacion,
                 responde_a_id, creado_en, actualizado_en)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (hilo_id, de, para, asunto, cuerpo, estado, requiere_confirmacion,
             responde_a_id, now_iso(), now_iso()),
        )
        mensaje_id = cur.lastrowid

        # Si el que respondio marca el mensaje original como respondido.
        if responde_a_id:
            db.execute(
                "UPDATE mensajes SET estado = 'respondido', actualizado_en = ?, "
                "respondido_en = ? WHERE id = ? AND estado = 'tomado'",
                (now_iso(), now_iso(), responde_a_id),
            )

        db.commit()
    except Exception:
        db.rollback()
        raise

    return jsonify(id=mensaje_id, hilo_id=hilo_id, estado=estado), 201


@app.route("/api/mensajes/pendientes", methods=["GET"])
@require_api_key
def mensajes_pendientes():
    maquina = request.args.get("maquina")
    if not maquina:
        return jsonify(error="falta el parametro 'maquina'"), 400
    limite = min(int(request.args.get("limite", 5)), 20)

    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        recuperar_tomados_vencidos(db)
        emitir_alertas(db)
        cur = db.execute(
            """
            SELECT id FROM mensajes
            WHERE estado = 'pendiente'
              AND de != ?
              AND (para = ? OR para IS NULL)
            ORDER BY creado_en ASC
            LIMIT ?
            """,
            (maquina, maquina, limite),
        )
        ids = [r["id"] for r in cur.fetchall()]

        if ids:
            placeholders = ",".join("?" for _ in ids)
            db.execute(
                f"UPDATE mensajes SET estado = 'tomado', tomado_por = ?, "
                f"tomado_en = COALESCE(tomado_en, ?), "
                f"actualizado_en = ? WHERE id IN ({placeholders})",
                (maquina, now_iso(), now_iso(), *ids),
            )
        db.commit()

        if ids:
            placeholders = ",".join("?" for _ in ids)
            cur = db.execute(
                f"SELECT * FROM mensajes WHERE id IN ({placeholders})", ids
            )
            resultado = [row_to_dict(r) for r in cur.fetchall()]
        else:
            resultado = []
    except Exception:
        db.rollback()
        raise

    return jsonify(mensajes=resultado)


@app.route("/api/mensajes/<int:mensaje_id>/fallar", methods=["POST"])
@require_api_key
def marcar_fallido(mensaje_id):
    data = request.get_json(force=True, silent=True) or {}
    error = data.get("error", "")

    db = get_db()
    cur = db.execute("SELECT intentos FROM mensajes WHERE id = ?", (mensaje_id,))
    row = cur.fetchone()
    if row is None:
        return jsonify(error="mensaje no encontrado"), 404

    intentos = row["intentos"] + 1
    nuevo_estado = "fallido" if intentos >= 3 else "pendiente"

    db.execute(
        "UPDATE mensajes SET intentos = ?, estado = ?, tomado_por = NULL, "
        "actualizado_en = ?, ultimo_error = ? WHERE id = ?",
        (intentos, nuevo_estado, now_iso(), (error or "")[:2000] or "(sin detalle)", mensaje_id),
    )
    db.commit()

    return jsonify(id=mensaje_id, intentos=intentos, estado=nuevo_estado, error=error)


@app.route("/api/mensajes/hilo/<hilo_id>", methods=["GET"])
@require_api_key
def ver_hilo(hilo_id):
    db = get_db()
    cur = db.execute(
        "SELECT * FROM mensajes WHERE hilo_id = ? ORDER BY creado_en ASC", (hilo_id,)
    )
    return jsonify(mensajes=[row_to_dict(r) for r in cur.fetchall()])


@app.route("/api/mensajes/esperando_humano", methods=["GET"])
@require_api_key
def esperando_humano():
    db = get_db()
    cur = db.execute(
        "SELECT * FROM mensajes WHERE estado = 'esperando_humano' "
        "ORDER BY creado_en ASC"
    )
    return jsonify(mensajes=[row_to_dict(r) for r in cur.fetchall()])


# ---------------------------------------------------------------------------
# Capacidades
# ---------------------------------------------------------------------------

@app.route("/api/capacidades/<maquina>", methods=["PUT"])
@require_api_key
def publicar_capacidades(maquina):
    data = request.get_json(force=True, silent=True) or {}
    descripcion = data.get("descripcion")
    if not descripcion:
        return jsonify(error="'descripcion' es obligatoria"), 400

    db = get_db()
    db.execute(
        """
        INSERT INTO capacidades (maquina, descripcion, actualizado_en)
        VALUES (?, ?, ?)
        ON CONFLICT(maquina) DO UPDATE SET
            descripcion = excluded.descripcion,
            actualizado_en = excluded.actualizado_en
        """,
        (maquina, descripcion, now_iso()),
    )
    db.commit()
    return jsonify(maquina=maquina, actualizado_en=now_iso())


@app.route("/api/capacidades", methods=["GET"])
@require_api_key
def listar_capacidades():
    db = get_db()
    cur = db.execute("SELECT * FROM capacidades ORDER BY maquina ASC")
    return jsonify(capacidades=[row_to_dict(r) for r in cur.fetchall()])


# ---------------------------------------------------------------------------
# Auditoria de invocaciones headless
# ---------------------------------------------------------------------------

@app.route("/api/invocaciones", methods=["POST"])
@require_api_key
def registrar_invocacion():
    data = request.get_json(force=True, silent=True) or {}
    maquina = data.get("maquina")
    mensaje_id = data.get("mensaje_id")
    resultado = data.get("resultado")
    resumen = data.get("resumen")

    if not maquina or not mensaje_id or resultado not in (
        "exito", "fallido", "esperando_humano"
    ):
        return jsonify(error="faltan campos o 'resultado' invalido"), 400

    db = get_db()
    db.execute(
        "INSERT INTO invocaciones (maquina, mensaje_id, resultado, resumen, creado_en) "
        "VALUES (?, ?, ?, ?, ?)",
        (maquina, mensaje_id, resultado, resumen, now_iso()),
    )
    db.commit()
    return jsonify(ok=True), 201


@app.route("/api/invocaciones", methods=["GET"])
@require_api_key
def listar_invocaciones():
    maquina = request.args.get("maquina")
    db = get_db()
    if maquina:
        cur = db.execute(
            "SELECT * FROM invocaciones WHERE maquina = ? ORDER BY creado_en DESC LIMIT 100",
            (maquina,),
        )
    else:
        cur = db.execute("SELECT * FROM invocaciones ORDER BY creado_en DESC LIMIT 100")
    return jsonify(invocaciones=[row_to_dict(r) for r in cur.fetchall()])


def _recibo(r, ahora=None):
    """Linea de tiempo de un mensaje: enviado -> leido (tomado por el destinatario) -> respondido."""
    ahora = ahora or datetime.now(timezone.utc)

    def min_desde(iso):
        t = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
        return round((ahora - t).total_seconds() / 60, 1)

    return {
        "id": r["id"], "hilo_id": r["hilo_id"], "de": r["de"], "para": r["para"],
        "asunto": r["asunto"], "estado": r["estado"], "intentos": r["intentos"],
        "enviado_en": r["creado_en"], "leido_en": r["tomado_en"], "leido_por": r["tomado_por"],
        "respondido_en": r["respondido_en"], "ultimo_error": r["ultimo_error"],
        "leido": r["tomado_en"] is not None,
        "minutos_sin_leer": min_desde(r["creado_en"]) if r["tomado_en"] is None and r["estado"] == "pendiente" else None,
    }


@app.route("/api/alertas", methods=["GET"])
@require_api_key
def alertas():
    """Alertas del broker para `para` (para sesiones interactivas que no tienen daemon).

    Corre emitir_alertas antes de leer, asi no depende de que otro daemon este polleando.
    ?marcar=1 las da por vistas (pasan a 'respondido') para no repetirlas.
    """
    para = request.args.get("para")
    if not para:
        return jsonify(error="falta el parametro 'para'"), 400
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        recuperar_tomados_vencidos(db)
        emitir_alertas(db)
        filas = db.execute(
            "SELECT id, hilo_id, asunto, cuerpo, creado_en FROM mensajes "
            "WHERE de = ? AND para = ? AND estado = 'pendiente' ORDER BY id", (ALERTA_DE, para)).fetchall()
        if request.args.get("marcar") and filas:
            ids = [r["id"] for r in filas]
            db.execute(
                f"UPDATE mensajes SET estado = 'respondido', tomado_por = ?, tomado_en = COALESCE(tomado_en, ?), "
                f"respondido_en = ?, actualizado_en = ? WHERE id IN ({','.join('?' for _ in ids)})",
                (para, now_iso(), now_iso(), now_iso(), *ids))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return jsonify(alertas=[row_to_dict(r) for r in filas])


@app.route("/api/mensajes/<int:mensaje_id>/recibo", methods=["GET"])
@require_api_key
def recibo(mensaje_id):
    r = get_db().execute("SELECT * FROM mensajes WHERE id = ?", (mensaje_id,)).fetchone()
    if r is None:
        return jsonify(error="mensaje no encontrado"), 404
    return jsonify(_recibo(r))


@app.route("/api/mensajes/enviados", methods=["GET"])
@require_api_key
def enviados():
    """Recibos de lo que mando `de` (mas recientes primero). ?solo_sin_leer=1 filtra los no leidos."""
    de = request.args.get("de")
    if not de:
        return jsonify(error="falta el parametro 'de'"), 400
    limite = min(int(request.args.get("limite", 20)), 100)
    sql = "SELECT * FROM mensajes WHERE de = ?"
    if request.args.get("solo_sin_leer"):
        sql += " AND tomado_en IS NULL AND estado = 'pendiente'"
    sql += " ORDER BY id DESC LIMIT ?"
    filas = get_db().execute(sql, (de, limite)).fetchall()
    return jsonify(mensajes=[_recibo(r) for r in filas])


@app.route("/api/estado", methods=["GET"])
@require_api_key
def estado():
    """Vista rapida tipo `rig ps` (idea de OpenRig): que tiene cada identidad.

    Solo lectura. Por identidad: mensajes tomados (con antiguedad en minutos,
    para ver colgados), pendientes dirigidos a ella, y su ultima invocacion.
    Aparte: hilos esperando humano y totales por estado.
    """
    db = get_db()
    ahora = datetime.now(timezone.utc)

    def minutos(iso):
        t = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
        return round((ahora - t).total_seconds() / 60, 1)

    identidades = {}

    def ident(nombre):
        return identidades.setdefault(
            nombre, {"tomados": [], "pendientes": 0, "ultima_invocacion": None}
        )

    for r in db.execute("SELECT maquina FROM capacidades"):
        ident(r["maquina"])
    for r in db.execute(
        "SELECT id, hilo_id, asunto, tomado_por, actualizado_en FROM mensajes "
        "WHERE estado = 'tomado'"
    ):
        ident(r["tomado_por"])["tomados"].append(
            {"id": r["id"], "hilo_id": r["hilo_id"], "asunto": r["asunto"],
             "minutos": minutos(r["actualizado_en"])}
        )
    for r in db.execute(
        "SELECT para, COUNT(*) AS n FROM mensajes WHERE estado = 'pendiente' "
        "AND para IS NOT NULL GROUP BY para"
    ):
        ident(r["para"])["pendientes"] = r["n"]
    broadcast = db.execute(
        "SELECT COUNT(*) AS n FROM mensajes WHERE estado = 'pendiente' AND para IS NULL"
    ).fetchone()["n"]
    for r in db.execute(
        "SELECT maquina, resultado, MAX(creado_en) AS cuando FROM invocaciones "
        "GROUP BY maquina"
    ):
        ident(r["maquina"])["ultima_invocacion"] = {
            "resultado": r["resultado"], "hace_min": minutos(r["cuando"])}

    por_estado = {r["estado"]: r["n"] for r in db.execute(
        "SELECT estado, COUNT(*) AS n FROM mensajes GROUP BY estado")}
    humano = [row_to_dict(r) for r in db.execute(
        "SELECT id, hilo_id, de, para, asunto FROM mensajes "
        "WHERE estado = 'esperando_humano' ORDER BY creado_en ASC")]

    sin_leer = [_recibo(r, ahora) for r in db.execute(
        "SELECT * FROM mensajes WHERE estado = 'pendiente' AND tomado_en IS NULL "
        "AND creado_en < ? ORDER BY creado_en ASC",
        ((ahora - timedelta(minutes=SIN_LEER_MIN)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),))]

    return jsonify(identidades=identidades, pendientes_broadcast=broadcast,
                   por_estado=por_estado, esperando_humano=humano, sin_leer=sin_leer)


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("MENSAJERIA_PORT", 8765)))
