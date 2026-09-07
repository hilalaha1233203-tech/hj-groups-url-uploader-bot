const { spawn } = require("node:child_process");

const python = process.env.PYTHON_BIN || "python3";
// start.py normalizes Voroa's API_ID/API_HASH/BOT_TOKEN names to the
// TELEGRAM_* names expected by the Python application.
const args = ["start.py"];

console.log(`[Voroa] Starting Telegram worker with ${python} ${args.join(" ")}`);

const child = spawn(python, args, {
  cwd: process.cwd(),
  env: process.env,
  stdio: "inherit",
});

child.on("error", (error) => {
  console.error("[Voroa] Failed to start Python worker:", error);
  process.exitCode = 1;
});

child.on("exit", (code, signal) => {
  if (signal) {
    console.error(`[Voroa] Python worker exited from signal ${signal}`);
    process.exitCode = 1;
  } else {
    console.log(`[Voroa] Python worker exited with code ${code ?? 1}`);
    process.exitCode = code ?? 1;
  }
});

const shutdown = (signal) => {
  console.log(`[Voroa] Received ${signal}; stopping Python worker`);
  child.kill(signal);
};

process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));
