#!/usr/bin/env bash
# Demo local de 2 minutos: levanta el broker, manda un mensaje, lo "reclama" como si fuera un daemon,
# lo responde, y muestra el recibo de lectura. No necesita ningun agente real.
# Uso: bash examples/quickstart.sh
set -euo pipefail
cd "$(dirname "$0")/../broker"
python3 -m venv .demo-venv >/dev/null && ./.demo-venv/bin/pip -q install -r requirements.txt
export MENSAJERIA_API_KEY=demo-key MENSAJERIA_DB_PATH=$(mktemp -d)/demo.db MENSAJERIA_WATCHDOG_SEG=0
./.demo-venv/bin/python -m flask --app app run -p 8799 >/dev/null 2>&1 &
PID=$!; trap 'kill $PID' EXIT; sleep 2
H=(-H "X-API-Key: demo-key" -H "Content-Type: application/json"); B=http://127.0.0.1:8799
echo "1) nodo-a le manda una tarea a nodo-b"
ID=$(curl -s "${H[@]}" -X POST $B/api/mensajes -d '{"de":"nodo-a","para":"nodo-b","asunto":"hola","cuerpo":"decime la hora"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
echo "2) recibo ANTES de que nodo-b lo lea:";  curl -s "${H[@]}" $B/api/mensajes/$ID/recibo | python3 -m json.tool | grep -E '"(estado|leido)"'
echo "3) nodo-b (su daemon) reclama pendientes:"; curl -s "${H[@]}" "$B/api/mensajes/pendientes?maquina=nodo-b" | python3 -c 'import json,sys;[print("   tomo #%d: %s"%(m["id"],m["cuerpo"])) for m in json.load(sys.stdin)["mensajes"]]'
echo "4) nodo-b responde:"; curl -s "${H[@]}" -X POST $B/api/mensajes -d "{\"de\":\"nodo-b\",\"para\":\"nodo-a\",\"cuerpo\":\"las 12:00\",\"responde_a_id\":$ID}" >/dev/null
echo "5) recibo DESPUES:"; curl -s "${H[@]}" $B/api/mensajes/$ID/recibo | python3 -m json.tool | grep -E '"(estado|leido|leido_por|respondido_en)"'
