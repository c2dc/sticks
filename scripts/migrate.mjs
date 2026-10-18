// Multiplataforma migrate runner: aplica as migracoes do Alembic
// (alembic upgrade head) usando o venv do backend. Uso: npm run db:migrate.

import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(scriptDir, "..");
const backendDir = path.join(root, "apps", "backend");
const isWindows = process.platform === "win32";
const venvBin = path.join(backendDir, ".venv", isWindows ? "Scripts" : "bin");
// Invoke via `python -m alembic` (robust: avoids the alembic.exe shim, which
// can exit 1 spuriously on some Windows shells).
const venvPython = path.join(venvBin, isWindows ? "python.exe" : "python");

if (!existsSync(venvPython)) {
  console.error(`[migrate] venv nao encontrado. Rode primeiro: npm run setup`);
  process.exit(1);
}

const r = spawnSync(venvPython, ["-m", "alembic", "upgrade", "head"], {
  stdio: "inherit",
  cwd: backendDir,
  env: process.env,
});
process.exit(r.status ?? 0);
