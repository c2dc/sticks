// Multiplataforma dev runner para a Pipeline_UI.
//
// Brings up backend (FastAPI/uvicorn) and frontend (Vite) at once, applying
// the Alembic migrations first. Written in Node (no shell syntax)
// to run the same on Windows, Linux and macOS:
//   * detects the venv in .venv/Scripts (Windows) or .venv/bin (Unix);
//   * invokes Python tools via `python -m <tool>` (robust across shells);
//   * uses the concurrently programmatic API (no shell, no arg splitting);
//   * the DB URL comes from the environment / the backend .env (not hardcoded here).
//
// Usage: npm run dev (see package.json).

import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import concurrently from "concurrently";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(scriptDir, "..");
const backendDir = path.join(root, "apps", "backend");
const isWindows = process.platform === "win32";

// Path of the venv python: Scripts on Windows, bin elsewhere. We invoke the
// backend tools as `python -m <tool>` so we never depend on the alembic/uvicorn
// console-script shims (which can exit 1 spuriously on some Windows shells).
const venvBin = path.join(backendDir, ".venv", isWindows ? "Scripts" : "bin");
const venvPython = path.join(venvBin, isWindows ? "python.exe" : "python");

if (!existsSync(venvPython)) {
  console.error(
    `\n[dev] backend venv python not found at ${venvPython}.\n` +
      `[dev] Run first:  npm run setup\n`
  );
  process.exit(1);
}

// The DB URL comes from the environment; if unset, the backend uses the default
// from .env/Settings.
const backendEnv = { ...process.env };

// 1) Migrations before serving (schema always present).
console.log("[dev] applying Alembic migrations...");
const migrate = spawn(venvPython, ["-m", "alembic", "upgrade", "head"], {
  stdio: "inherit",
  cwd: backendDir,
  env: backendEnv,
});

migrate.on("exit", (code) => {
  if (code !== 0) {
    console.error(`[dev] alembic upgrade failed (code ${code}).`);
    process.exit(code ?? 1);
  }

  // 2) Backend + frontend in parallel via the concurrently API.
  // Each command has its own cwd; the backend argv goes as a single quoted
  // string for the concurrently shell (avoids splitting on Windows).
  const q = (s) => (isWindows ? `"${s}"` : `'${s}'`);

  const { result } = concurrently(
    [
      {
        name: "backend",
        command: `${q(venvPython)} -m uvicorn app.main:app --host 127.0.0.1 --port 9080 --reload`,
        cwd: backendDir,
        env: backendEnv,
        prefixColor: "cyan",
      },
      {
        name: "frontend",
        command: "npm --silent run dev --workspace apps/frontend",
        cwd: root,
        prefixColor: "magenta",
      },
    ],
    {
      killOthersOn: ["failure"],
      prefix: "name",
    }
  );

  result.then(
    () => process.exit(0),
    () => process.exit(1)
  );
});
