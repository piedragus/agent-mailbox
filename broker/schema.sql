-- Schema del broker de mensajeria.
-- Ledger de mensajes (nunca se borran, solo cambian de estado) + tabla de capacidades por maquina.

CREATE TABLE IF NOT EXISTS mensajes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hilo_id TEXT NOT NULL,
    de TEXT NOT NULL,
    para TEXT,                          -- NULL = broadcast a todas las maquinas
    asunto TEXT,
    cuerpo TEXT NOT NULL,
    estado TEXT NOT NULL DEFAULT 'pendiente'
        CHECK (estado IN ('pendiente', 'tomado', 'respondido', 'esperando_humano', 'fallido', 'descartado')),
    requiere_confirmacion INTEGER NOT NULL DEFAULT 0 CHECK (requiere_confirmacion IN (0, 1)),
    tomado_por TEXT,
    intentos INTEGER NOT NULL DEFAULT 0,
    responde_a_id INTEGER REFERENCES mensajes(id),
    creado_en TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    actualizado_en TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_mensajes_hilo ON mensajes(hilo_id);
CREATE INDEX IF NOT EXISTS idx_mensajes_pendientes ON mensajes(estado, para);

CREATE TABLE IF NOT EXISTS capacidades (
    maquina TEXT PRIMARY KEY,
    descripcion TEXT NOT NULL,          -- contenido markdown del CAPABILITIES.md
    actualizado_en TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Log de auditoria: una fila por cada invocacion headless de Claude Code que
-- dispara un daemon, exitosa o no. El rate limit de invocaciones por hora lo
-- hace el daemon localmente (no depende de esta tabla); esta tabla es historial
-- consultable, no un contador de control.
CREATE TABLE IF NOT EXISTS invocaciones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    maquina TEXT NOT NULL,
    mensaje_id INTEGER NOT NULL REFERENCES mensajes(id),
    resultado TEXT NOT NULL CHECK (resultado IN ('exito', 'fallido', 'esperando_humano')),
    resumen TEXT,
    creado_en TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_invocaciones_maquina_fecha ON invocaciones(maquina, creado_en);
