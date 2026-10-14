# Protocolo de mensajeria

## Identidad = agente, no máquina física (12/sep)

Cada valor de `de`/`para` es una **identidad de agente**, no necesariamente
una máquina física distinta. Si un mismo host corre más de un agente (ej.
Claude Code y Codex en el host), cada uno se registra con su propio nombre
(`nodo-a-claude`, `nodo-a-codex`) y publica sus propias capacidades. El
broker no sabe ni le importa si dos identidades comparten hardware — las
reglas de abajo (anti-loop, auto-mensaje, etc.) se aplican igual por nombre
de identidad.

## Rol coordinador (12/sep)

Una identidad puede tener `MENSAJERIA_ROL=coordinador` en vez del default
`trabajador`. No resuelve tareas — decide a qué identidad conviene
asignarlas (optimizando costo/beneficio según lo que cada una publicó en
`capacidades`) y le redacta el pedido. Pensado para correr con un modelo más
caro (`MENSAJERIA_CLAUDE_MODEL=claude-opus-5`) reservado solo para triar y
para los puntos sensibles — el volumen de trabajo real lo hacen identidades
más baratas.

Formato de respuesta esperado del coordinador (si no viene así, el daemon
cae al flujo normal — típicamente porque decidió frenar con
`REQUIERE_HUMANO`):
```
ASIGNAR_A: <identidad>
TAREA:
<el pedido completo para esa identidad>
```

El daemon, al ver ese formato, postea DOS mensajes en el mismo hilo: el
pedido real a la identidad elegida (esto es lo que marca el mensaje
original como respondido), y un aviso corto de vuelta a quien preguntó
("Asignado a X"). La resolución final de la identidad elegida llega
después, al mismo hilo — un solo `GET /api/mensajes/hilo/<id>` muestra la
cadena completa: pedido → decisión de ruteo → resolución.

Límite conocido de esta primera versión: el coordinador no reenvía
automáticamente la resolución final de vuelta al que preguntó originalmente
— queda en el mismo hilo, visible, pero no como una respuesta directa a esa
persona/identidad. Suficiente para v1, revisar si hace falta más adelante.

## Ciclo de vida de un mensaje

```
pendiente --(daemon lo reclama)--> tomado --(exito)--> respondido
    |                                  |
    |                              (falla, intentos<3)
    |                                  v
    |                              pendiente (reintenta)
    |                                  |
    |                              (falla, intentos>=3)
    |                                  v
    |                               fallido
    |
    +--(requiere_confirmacion=1, o hilo pasa MAX_MSGS_POR_HILO)--> esperando_humano
```

**Recuperación de mensajes colgados (idea de OpenRig).** Si un daemon reclama
un mensaje y muere antes de responder, el mensaje queda en `tomado`. Cuando
cualquier daemon consulta `/pendientes`, el broker devuelve a `pendiente` los
`tomado` que llevan más de `MENSAJERIA_TOMADO_TIMEOUT_MIN` minutos (default
30, tiene que ser mayor al timeout de una invocación headless). Cada rescate
cuenta como un intento: al tercero pasa a `fallido`, igual que `/fallar`.

`esperando_humano` es un estado terminal para el automatismo: ningun daemon
lo reclama. Se resuelve a mano (el humano lee `/api/mensajes/esperando_humano`
o `/api/mensajes/hilo/<id>`, decide, y si hace falta postea la continuacion
el mismo via `POST /api/mensajes` con `de` = su propio nombre de maquina, o
simplemente actua fuera del sistema).

## Reglas duras (no configurables sin tocar el broker)

- Una maquina nunca puede reclamar un mensaje que ella misma mando (`de != maquina`
  en la query de pendientes).
- Un mensaje con `requiere_confirmacion=1` nunca aparece en `/pendientes`.
- Un hilo (`hilo_id`) que supera `MENSAJERIA_MAX_MSGS_POR_HILO` (default 10)
  fuerza el siguiente mensaje a `esperando_humano`, sin importar lo que haya
  pedido el que lo mando. Esto es lo que corta un loop A<->B contestandose
  para siempre.

## Formato de un mensaje

```json
{
  "de": "nodo-b",
  "para": "nodo-a",           // null = broadcast, cualquiera (menos el emisor) lo puede tomar
  "asunto": "GPU libre?",
  "cuerpo": "Necesito correr un batch de embeddings, tenes la CMP 70HX libre?",
  "requiere_confirmacion": false,
  "hilo_id": "uuid-opcional",  // si no se manda, se genera uno nuevo
  "responde_a_id": null        // id del mensaje que se esta respondiendo
}
```

## Cuando usar `requiere_confirmacion: true`

Lo decide el que MANDA el mensaje (sabe que esta pidiendo algo delicado) O el
daemon que lo RECIBE (si Claude Code headless concluye que la tarea es
irreversible, responde con el marcador `REQUIERE_HUMANO:` y el daemon
convierte eso en una respuesta con `requiere_confirmacion=true`). Las dos
vias llevan al mismo lugar: el hilo queda esperando que el humano lo mire.

Ejemplos de "esto va con requiere_confirmacion":
- Cualquier accion contra una base de datos de produccion.
- Cualquier `git push` a `main`/`master` en un repo que no sea de scratch.
- Enviar un mensaje a un cliente (WhatsApp/mail) en nombre de la organizacion.
- Tocar configuracion de infraestructura que si sale mal tira un servicio
  (systemd units, firewall, DNS).

## Estado de un vistazo

`GET /api/estado` (solo lectura) devuelve, por identidad, los mensajes que
tiene `tomado` con su antigüedad en minutos (un número alto = daemon
colgado), cuántos `pendiente` le esperan y su última invocación; más los
pendientes broadcast, totales por estado y la lista de hilos
`esperando_humano`. Es el equivalente a `rig ps`: primer lugar donde mirar
cuando "no pasa nada".

## Patrón dueño + revisor (convención, idea de OpenRig)

Para trabajo que toca algo importante, no alcanza con que una identidad diga
"hecho": otra verifica el resultado exacto. Se hace con mensajes normales, sin
cambios al broker:

1. El **dueño** resuelve y responde en el hilo con el candidato concreto
   (commit, ruta, diff, salida de tests), no con un resumen.
2. Postea un mensaje `para=<revisor>` en el MISMO `hilo_id` con asunto
   `REVISAR: <tema>` que cita ese candidato exacto.
3. El **revisor** contesta `APROBADO` o `RECHAZADO: <motivo>` sobre ese
   candidato. Un rechazo vuelve al dueño en el mismo hilo.
4. Lo irreversible sigue yendo con `requiere_confirmacion`: el revisor
   automático no reemplaza a el humano en esos casos.

Regla: dueño y revisor tienen que ser identidades distintas (idealmente de
distinto agente, ej. `nodo-a-claude` revisa lo de `nodo-a-codex`). El tope
`MENSAJERIA_MAX_MSGS_POR_HILO` sigue aplicando y corta ping-pongs de rechazos.

## Auditoria

Todo lo que se ejecuto en automatico (exitoso, fallido, o frenado por
requiere-humano) queda en la tabla `invocaciones`, consultable via
`GET /api/invocaciones?maquina=X`. Sirve para revisar despues "que hizo cada
maquina sola esta semana" sin tener que leer todos los hilos.

## Recibos de lectura
Cada mensaje guarda `tomado_en` (cuando el destinatario lo reclamo = "leido"), `respondido_en` y `ultimo_error`.
- `GET /api/mensajes/<id>/recibo` -> linea de tiempo: enviado_en, leido, leido_en, leido_por, respondido_en, estado, intentos, ultimo_error.
- `GET /api/mensajes/enviados?de=<yo>[&solo_sin_leer=1][&limite=N]` -> recibos de lo que mande.
- `GET /api/estado` -> campo `sin_leer`: pendientes que nadie tomo hace mas de `MENSAJERIA_SIN_LEER_MIN` (10) minutos
  (destinatario caido o sin sesion). Mirarlo antes de asumir que "no contestan".
