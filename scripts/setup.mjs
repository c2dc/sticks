// Multiplataforma setup runner para a Pipeline_UI.
//
// Prepara o ambiente nativo uma unica vez:
//   1) cria o venv do backend em ui/backend/.venv;
//   2) instala o backend em modo editavel com as deps de dev;
//   3) instala as deps do frontend (npm install na raiz).
//
// Escrito em Node para rodar igual em Windows, Linux e macOS (detecta
// o python/python3 e o layout do venv). Uso: npm run setup.

import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(scriptDir, "..");
const backendDir = path.join(root, "ui", "backend");
const isWindows = process.platform === "win32";

function sh(cmd, args, opts = {}) {
  console.log(`[setup] $ ${cmd} ${args.join(" ")}`);
  const r = spawnSync(cmd, args, { stdio: "inherit", shell: isWindows, ...opts });
  if (r.status !== 0) {
    console.error(`[setup] falhou: ${cmd} (codigo ${r.status})`);
    process.exit(r.status ?? 1);
  }
}

// Escolhe o interpretador Python disponivel.
function pickPython() {
  const candidates = isWindows ? ["python", "py"] : ["python3", "python"];
  for (const c of candidates) {
    const r = spawnSync(c, ["--version"], { stdio: "ignore", shell: isWindows });
    if (r.status === 0) return c;
  }
  console.error("[setup] Python nao encontrado no PATH. Instale o Python 3.11+.");
  process.exit(1);
}

const python = pickPython();
const venvDir = path.join(backendDir, ".venv");
const venvBin = path.join(venvDir, isWindows ? "Scripts" : "bin");
const venvPy = path.join(venvBin, isWindows ? "python.exe" : "python");

// 1) cria o venv se ainda nao existe.
if (!existsSync(venvPy)) {
  sh(python, ["-m", "venv", venvDir]);
} else {
  console.log("[setup] venv ja existe, reutilizando.");
}

// 2) instala o backend editavel + deps de dev usando o python do venv.
sh(venvPy, ["-m", "pip", "install", "--upgrade", "pip"], { cwd: backendDir });
sh(venvPy, ["-m", "pip", "install", "-e", ".[dev]"], { cwd: backendDir });

// 3) deps do frontend (workspaces) na raiz.
sh("npm", ["install"], { cwd: root });

console.log("\n[setup] concluido. Suba o Postgres (docker compose up -d postgres) e rode: npm run dev.");

