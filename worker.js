const { spawn, spawnSync } = require("node:child_process");

const candidates = [process.env.PYTHON_BIN, "python3", "python"].filter(Boolean);

function findPython() {
  for (const command of candidates) {
    const result = spawnSync(command, ["--version"], {
      stdio: "pipe",
      encoding: "utf8",
    });
    if (result.status === 0) return command;
  }
  throw new Error(`Python executable not found. Tried: ${candidates.join(", ")}`);
}

const python = findPython();
const args = ["-u", "start.py"];
let child = null;
let stopping = false;
let restartTimer = null;

console.log(`[Voroa] Launcher ready: ${python} ${args.join(" ")}`);
console.log(`[Voroa] Node ${process.version}; working directory ${process.cwd()}`);

function startPython() {
  if (stopping) return;

  console.log(`[Voroa] Starting Python worker: ${python} ${args.join(" ")}`);
  child = spawn(python, args, {
    cwd: process.cwd(),
    env: process.env,
    stdio: "inherit",
  });

  child.on("error", (error) => {
    console.error("[Voroa] Python process error:", error);
    scheduleRestart();
  });

  child.on("exit", (code, signal) => {
    child = null;
    if (stopping) return;
    console.error(
      `[Voroa] Python worker stopped (code=${code ?? "null"}, signal=${signal ?? "none"}). Restarting in 5s...`,
    );
    scheduleRestart();
  });
}

function scheduleRestart() {
  if (stopping || restartTimer) return;
  restartTimer = setTimeout(() => {
    restartTimer = null;
    startPython();
  }, 5000);
}

function shutdown(signal) {
  if (stopping) return;
  stopping = true;
  if (restartTimer) clearTimeout(restartTimer);
  restartTimer = null;
  console.log(`[Voroa] Received ${signal}; stopping Python worker`);
  if (child) child.kill(signal);
}

process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));

startPython();
