// Multiplataforma dev runner para a Pipeline_UI.
//
// Brings up backend (FastAPI/uvicorn) and frontend (Vite) at once, applying
// the Alembic migrations first. Written in Node (no shell syntax)
// to run the same on Windows, Linux and macOS:
//   * detects the venv in .venv/Scripts (Windows) or .venv/bin (Unix);
//   * invokes Python tools via `python -m <tool>` (robust across shells);
//   * spawns backend and frontend directly (no shell, no arg splitting);
//   * the DB URL comes from the environment / the backend .env (not hardcoded here).
//
// Native hot reload: the database runs in a container (npm run db:up), backend and
// frontend run on the host. On Windows the uvicorn reloader sends CTRL_C_EVENT to
// every process on its console, which used to take the frontend down. The backend
// therefore runs detached (own hidden console), so the signal never reaches the
// frontend. Each exit is logged and no process kills the others.
//
// Usage: npm run dev | npm run dev:backend | npm run dev:frontend (see package.json).

import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

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

  const only = process.argv.includes("--backend-only")
    ? "backend"
    : process.argv.includes("--frontend-only")
      ? "frontend"
      : null;

  const children = [];

  const pipeWithPrefix = (stream, name, out) => {
    let pending = "";
    stream.on("data", (chunk) => {
      const lines = (pending + chunk.toString()).split(/\r?\n/);
      pending = lines.pop() ?? "";
      for (const line of lines) out.write(`[${name}] ${line}\n`);
    });
  };

  const start = (name, command, args, options) => {
    const child = spawn(command, args, { ...options, stdio: ["ignore", "pipe", "pipe"] });
    children.push(child);
    pipeWithPrefix(child.stdout, name, process.stdout);
    pipeWithPrefix(child.stderr, name, process.stderr);
    child.on("exit", (exitCode, signal) => {
      console.log(`[dev] ${name} exited with code ${exitCode ?? signal}`);
    });
    return child;
  };

  if (only !== "frontend") {
    // detached: own console/process group on Windows, so the reloader's
    // CTRL_C_EVENT stays inside the backend.
    start(
      "backend",
      venvPython,
      ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8010", "--reload", "--reload-dir", "app"],
      { cwd: backendDir, env: backendEnv, detached: true, windowsHide: true }
    );
  }

  if (only !== "backend") {
    // Run Vite with node directly: no npm.cmd/cmd.exe wrapper to prompt "Terminate batch job".
    const vite = path.join(root, "node_modules", "vite", "bin", "vite.js");
    start("frontend", process.execPath, [vite], {
      cwd: path.join(root, "apps", "frontend"),
      env: process.env,
    });
  }

  const stopAll = () => {
    for (const child of children) {
      if (child.exitCode !== null || !child.pid) continue;
      if (isWindows) {
        spawn("taskkill", ["/pid", String(child.pid), "/t", "/f"], { stdio: "ignore" });
      } else {
        child.kill("SIGTERM");
      }
    }
  };
  process.on("SIGINT", () => {
    stopAll();
    setTimeout(() => process.exit(0), 1500);
  });
  process.on("SIGTERM", () => {
    stopAll();
    setTimeout(() => process.exit(0), 1500);
  });
});