// Un app de PM2 por identidad -- escanea /etc/mensajeria/identidades/*.env
// y crea una entrada por archivo. Sumar una identidad nueva es agregar un
// .env ahi y correr `pm2 reload ecosystem.config.js`, NO editar este
// archivo a mano.
const fs = require("fs");
const path = require("path");

const DIR_IDENTIDADES = "/etc/mensajeria/identidades";

function leerEnvFile(ruta) {
  const env = {};
  for (const linea of fs.readFileSync(ruta, "utf8").split("\n")) {
    const l = linea.trim();
    if (!l || l.startsWith("#")) continue;
    const i = l.indexOf("=");
    if (i === -1) continue;
    env[l.slice(0, i).trim()] = l.slice(i + 1).trim();
  }
  return env;
}

let apps = [];
if (fs.existsSync(DIR_IDENTIDADES)) {
  apps = fs
    .readdirSync(DIR_IDENTIDADES)
    .filter((f) => f.endsWith(".env"))
    .map((f) => {
      const identidad = f.replace(/\.env$/, "");
      return {
        name: `mensajeria-${identidad}`,
        cwd: __dirname,
        script: "venv/bin/python3",
        args: "daemon_loop.py",
        interpreter: "none",
        env: leerEnvFile(path.join(DIR_IDENTIDADES, f)),
        autorestart: true,
        max_restarts: 20,
        restart_delay: 5000,
        out_file: `/var/log/mensajeria/${identidad}.out.log`,
        error_file: `/var/log/mensajeria/${identidad}.err.log`,
      };
    });
}

module.exports = { apps };
