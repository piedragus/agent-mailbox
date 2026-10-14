// PM2 no soporta "env_file" nativo -- se parsea el .env a mano y se pasa
// como objeto "env", que si soporta.
const fs = require("fs");

function leerEnvFile(ruta) {
  const env = {};
  if (!fs.existsSync(ruta)) return env;
  for (const linea of fs.readFileSync(ruta, "utf8").split("\n")) {
    const l = linea.trim();
    if (!l || l.startsWith("#")) continue;
    const i = l.indexOf("=");
    if (i === -1) continue;
    env[l.slice(0, i).trim()] = l.slice(i + 1).trim();
  }
  return env;
}

const env = leerEnvFile("/etc/mensajeria/broker.env");

// MENSAJERIA_BIND_HOST (14/sep): antes hardcodeado a 127.0.0.1 -- eso es lo
// que impedia que cualquier otra maquina (nodo-b, etc) le hablara al
// broker. Default sigue siendo 127.0.0.1 (mas seguro, ninguna maquina nueva
// se suma sola); para habilitar acceso desde la Tailnet, poner en
// broker.env la IP Tailscale de ESTA maquina (nunca 0.0.0.0 -- bindear a la
// IP puntual de la interfaz tailscale0 es lo que garantiza que solo se
// pueda llegar via la Tailnet, sin depender de reglas de firewall aparte).
const bindHost = env.MENSAJERIA_BIND_HOST || "127.0.0.1";
const port = env.MENSAJERIA_PORT || "8765";

module.exports = {
  apps: [
    {
      name: "mensajeria-broker",
      cwd: __dirname,
      script: "venv/bin/gunicorn",
      args: `-w 2 -b ${bindHost}:${port} app:app`,
      interpreter: "none",
      env,
      autorestart: true,
      max_restarts: 20,
      restart_delay: 3000,
      out_file: "/var/log/mensajeria/broker.out.log",
      error_file: "/var/log/mensajeria/broker.err.log",
    },
  ],
};
