# Capacidades de <IDENTIDAD>

Este archivo lo lee el daemon local en cada pasada y se publica en el broker
via `PUT /api/capacidades/<IDENTIDAD>` para que las demas identidades (y el
coordinador, si hay uno) sepan que pueden pedirle a esta. Mantenerlo corto y
concreto -- es contexto que se inyecta en CADA invocacion headless de todas
las demas identidades.

## Agente y costo relativo
- Agente: <Claude Code | Codex | OpenHands | ...>
- Costo relativo: <barato | estandar | caro> -- el coordinador usa esto para
  decidir a quien asignarle una tarea optimizando costo/beneficio. Si esta
  identidad es la mas cara del sistema (ej. el coordinador mismo con Opus),
  aclararlo para que nadie mas le delegue tareas simples de vuelta.

## Hardware
- CPU / RAM / GPU relevante
- Storage relevante (donde vive que, capacidad libre)

## Repos y proyectos que vive/gestiona esta identidad
- repo -> que hace, en que estado esta

## Servicios corriendo aca
- servicio -> para que sirve, como se reinicia

## Que puede resolver esta identidad que otras no
- Ej: "unica con acceso directo a la GPU X", "unica con la DB de produccion Y"

## Que NO puede/debe hacer esta identidad
- Limitaciones explicitas -- ayuda a que otras identidades (o el
  coordinador) no le pidan/asignen algo que no corresponde (p.ej. "no tiene
  acceso a internet salvo dominios whitelisteados", "solo tareas simples de
  bajo riesgo")
