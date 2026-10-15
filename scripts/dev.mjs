// Multiplataforma dev runner para a Pipeline_UI.
//
// Sobe backend (FastAPI/uvicorn) e frontend (Vite) de uma vez, aplicando
// antes as migracoes do Alembic. Escrito em Node (sem sintaxe de shell)
// para rodar igual em Windows, Linux e macOS:
//   * detecta o venv em .venv/Scripts (Windows) ou .venv/bin (Unix);
//   * usa a API programatica do concurrently (sem shell, sem quebra de args);
//   * a URL do banco vem do ambiente / do .env do backend (nao e cravada aqui).
//
// Uso: npm run dev (ver package.json).

import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import concurrently from "concurrently";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(scriptDir, "..");
const backendDir = path.join(root, "ui", "backend");
const isWindows = process.platform === "win32";

// Caminho dos executaveis do venv: Scripts no Windows, bin no restante.
const venvBin = path.join(backendDir, ".venv", isWindows ? "Scripts" : "bin");
const exe = (name) => path.join(venvBin, isWindows ? `${name}.exe` : name);

const alembic = exe("alembic");
const uvicorn = exe("uvicorn");

if (!existsSync(alembic) || !existsSync(uvicorn)) {
  console.error(
    `\n[dev] venv do backend nao encontrado em ${venvBin}.\n` +
      `[dev] Rode primeiro:  npm run setup\n`
  );
  process.exit(1);
}

// A URL do banco vem do ambiente; se nao definida, o backend usa o default
// do .env/Settings.
const backendEnv = { ...process.env };

// 1) Migracoes antes de servir (schema sempre presente).
console.log("[dev] aplicando migracoes do Alembic...");
const migrate = spawn(alembic, ["upgrade", "head"], {
  stdio: "inherit",
  cwd: backendDir,
  env: backendEnv,
});

migrate.on("exit", (code) => {
  if (code !== 0) {
    console.error(`[dev] alembic upgrade falhou (codigo ${code}).`);
    process.exit(code ?? 1);
  }

  // 2) Backend + frontend em paralelo via API do concurrently.
  // Cada comando tem seu proprio cwd; o argv do backend vai como string
  // unica citada para o shell do concurrently (evita quebra no Windows).
  const q = (s) => (isWindows ? `"${s}"` : `'${s}'`);

  const { result } = concurrently(
    [
      {
        name: "backend",
        command: `${q(uvicorn)} app.main:app --host 127.0.0.1 --port 8010 --reload`,
        cwd: backendDir,
        env: backendEnv,
        prefixColor: "cyan",
      },
      {
        name: "frontend",
        command: "npm --silent run dev --workspace ui/frontend",
        cwd: root,
        prefixColor: "magenta",
      },
    ],
    {
      killOthers: ["failure"],
      prefix: "name",
    }
  );

  result.then(
    () => process.exit(0),
    () => process.exit(1)
  );
});
