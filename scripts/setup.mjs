// Cross-platform setup runner for the Pipeline_UI.
//
// Prepares the native environment once:
//   1) creates the backend venv at apps/backend/.venv;
//   2) installs the backend in editable mode with the dev deps;
//   3) installs the frontend deps (npm install at the root).
//
// Written in Node to run the same on Windows, Linux and macOS (detects
// python/python3 and the venv layout). Usage: npm run setup.

import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(scriptDir, "..");
const backendDir = path.join(root, "apps", "backend");
const isWindows = process.platform === "win32";

function sh(cmd, args, opts = {}) {
  console.log(`[setup] $ ${cmd} ${args.join(" ")}`);
  const r = spawnSync(cmd, args, { stdio: "inherit", shell: isWindows, ...opts });
  if (r.status !== 0) {
    console.error(`[setup] failed: ${cmd} (code ${r.status})`);
    process.exit(r.status ?? 1);
  }
}

// Pick the available Python interpreter.
function pickPython() {
  const candidates = isWindows ? ["python", "py"] : ["python3", "python"];
  for (const c of candidates) {
    const r = spawnSync(c, ["--version"], { stdio: "ignore", shell: isWindows });
    if (r.status === 0) return c;
  }
  console.error("[setup] Python not found on PATH. Install Python 3.11+.");
  process.exit(1);
}

const python = pickPython();
const venvDir = path.join(backendDir, ".venv");
const venvBin = path.join(venvDir, isWindows ? "Scripts" : "bin");
const venvPy = path.join(venvBin, isWindows ? "python.exe" : "python");

// 1) create the venv if it does not exist yet.
if (!existsSync(venvPy)) {
  sh(python, ["-m", "venv", venvDir]);
} else {
  console.log("[setup] venv already exists, reusing.");
}

// 2) install the editable backend + dev deps using the venv python.
sh(venvPy, ["-m", "pip", "install", "--upgrade", "pip"], { cwd: backendDir });
sh(venvPy, ["-m", "pip", "install", "-e", ".[dev]"], { cwd: backendDir });

// 3) frontend deps (workspaces) at the root.
sh("npm", ["install"], { cwd: root });

console.log("\n[setup] done. Bring up Postgres (docker compose up -d postgres) and run: npm run dev.");
