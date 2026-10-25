#!/usr/bin/env node
// Controlled transport fixture. `run` and `serve` delegate to real OpenCode.
// fixture-run is deliberately local and MUST NOT be reported as model inference.
import fs from "node:fs";
import { spawnSync } from "node:child_process";
const [verb, ...args] = process.argv.slice(2);
if (verb === "--version") {
  console.log("awb-opencode-fixture/1.0.0");
} else if (verb === "fixture-run") {
  const prompt = fs.readFileSync(0, "utf8");
  if (!prompt || !args.includes("--attach") || !args.includes("--model")) process.exit(2);
  fs.writeFileSync("main.txt", "controlled wrapper fixture change\n");
  console.log(JSON.stringify({ type: "text", part: { text: "CONTROLLED_FIXTURE_NOT_MODEL: " + (process.env.AGENT_FIXTURE_TOKEN || "no-fixture-token") } }));
  console.log(JSON.stringify({ type: "step_finish", part: { reason: "stop" } }));
} else {
  const child = spawnSync("opencode", [verb, ...args], { stdio: "inherit" });
  process.exit(child.status ?? 1);
}
