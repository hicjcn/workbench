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
} else if (verb === "fixture-multi-run") {
  const prompt = fs.readFileSync(0, "utf8");
  if (!prompt || !args.includes("--attach") || !args.includes("--model")) process.exit(2);
  const repositories = ["order service", "services/payment 中文"];
  for (const repository of repositories) fs.readFileSync(repository + "/main.txt");
  let report;
  if (prompt.startsWith("只读分析")) {
    report = { summary: "CONTROLLED_FIXTURE_NOT_MODEL: joint analysis", impacted_paths: repositories.map(path => path + "/main.txt"), plan: "Apply the controlled fixture change to both repositories", risks: [] };
  } else if (prompt.startsWith("使用已安装")) {
    for (const repository of repositories) fs.writeFileSync(repository + "/main.txt", "controlled multirepo change\n");
    report = { summary: "CONTROLLED_FIXTURE_NOT_MODEL: two repository edits", verification_notes: "Run the separate per-repository verification step" };
  } else if (prompt.startsWith("生成服务端概要设计")) {
    report = { title: "CONTROLLED_FIXTURE_NOT_MODEL: overview", markdown: "# Multi repository fixture\n\nBoth independent repositories are visible in the shared parent. This document is a controlled transport fixture." };
  } else {
    report = { summary: "CONTROLLED_FIXTURE_NOT_MODEL: independent joint review", findings: [] };
  }
  console.log(JSON.stringify({ type: "text", part: { text: JSON.stringify(report) } }));
  console.log(JSON.stringify({ type: "step_finish", part: { reason: "stop" } }));
} else {
  const child = spawnSync("opencode", [verb, ...args], { stdio: "inherit" });
  process.exit(child.status ?? 1);
}
