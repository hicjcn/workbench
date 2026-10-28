import assert from "node:assert/strict";
import { test } from "node:test";
import {
  currentRequirementStep,
  preparationAction,
} from "../src/requirementSteps.ts";
import type { Detail } from "../src/api.ts";
import type { WorkflowState } from "../src/WorkflowPanel.tsx";
import type { MaterialState } from "../src/materialTypes.ts";

const detail = (changes: Partial<Detail> = {}): Detail =>
  ({
    workspace: { environment: "STOPPED", snapshot: { tree_digest: "code" } },
    profile: null,
    jobs: [],
    ...changes,
  }) as Detail;
const workflow = (state: string, next_step: string | null): WorkflowState =>
  ({
    current: { id: "flow-1", state, next_step },
  }) as WorkflowState;
const prepared = () =>
  detail({
    jobs: [{ action: "start", status: "SUCCEEDED" } as Detail["jobs"][number]],
  });
const accepted = (changes = {}): MaterialState =>
  ({
    current: {
      flow_id: "flow-1",
      context_valid: true,
      accepted: true,
      ...changes,
    },
  }) as MaterialState;

test("new requirements first ask for a source copy", () => {
  const value = detail({
    workspace: {
      environment: "NOT_CREATED",
      snapshot: null,
    } as Detail["workspace"],
  });
  assert.equal(currentRequirementStep(value, null, null), 0);
  assert.equal(preparationAction(value).action, "prepare");
});
test("failed prepare remains retryable even when environment is ERROR", () => {
  const value = detail({
    workspace: { environment: "ERROR", snapshot: null } as Detail["workspace"],
  });
  assert.equal(currentRequirementStep(value, null, null), 0);
  assert.equal(preparationAction(value).action, "prepare");
});
test("fixed profiles require preflight before starting the runtime", () => {
  const value = detail({
    profile: {} as Detail["profile"],
    preflight: { status: "FAILED" } as Detail["preflight"],
  });
  assert.equal(currentRequirementStep(value, null, null), 0);
  assert.equal(preparationAction(value).action, "preflight");
  assert.equal(
    preparationAction({
      ...value,
      preflight: { status: "PASSED" } as Detail["preflight"],
    }).action,
    "start",
  );
});
test("preflight explains how to recover an already running environment", () => {
  const value = detail({
    profile: {} as Detail["profile"],
    workspace: { environment: "RUNNING", snapshot: {} } as Detail["workspace"],
  });
  assert.equal(preparationAction(value).action, "stop");
});
test("stopping a previously prepared environment does not lose progress", () => {
  assert.equal(currentRequirementStep(detail(), null, null), 0);
  assert.equal(currentRequirementStep(prepared(), null, null), 1);
});
test("a diagnostic container requires stopping before normal preparation and never counts as an Agent runtime", () => {
  for (const preflight of [null, { status: "FAILED" }, { status: "PASSED" }]) {
    const value = {
      ...prepared(),
      profile: {} as Detail["profile"],
      preflight: preflight as Detail["preflight"],
      workspace: { environment: "DEBUG_RUNNING", snapshot: {} } as Detail["workspace"],
      jobs: [{ action: "force_start", status: "SUCCEEDED" } as Detail["jobs"][number]],
    };
    assert.equal(currentRequirementStep(value, null, null), 0);
    assert.equal(preparationAction(value).action, "stop");
  }
});
test("analysis and human scope confirmation stay in one stage", () => {
  for (const key of ["analysis", "scope"])
    assert.equal(
      currentRequirementStep(prepared(), workflow("ACTIVE", key), null),
      1,
    );
});
test("development and verification failures stay at the development stage", () => {
  for (const key of ["development", "verification"])
    assert.equal(
      currentRequirementStep(detail(), workflow("ACTIVE", key), null),
      2,
    );
});
test("all reviewer counts, issue disposition and self review stay at review", () => {
  for (const key of [
    "model_review_0",
    "model_review_3",
    "resolution",
    "self_review",
  ])
    assert.equal(
      currentRequirementStep(detail(), workflow("ACTIVE", key), null),
      3,
    );
});
test("stale or rejected flows return to analysis while keeping saved history", () => {
  for (const state of ["STALE", "REJECTED", "SUPERSEDED"])
    assert.equal(
      currentRequirementStep(detail(), workflow(state, null), accepted()),
      1,
    );
});
test("self review unlocks design, not completed delivery", () => {
  assert.equal(
    currentRequirementStep(detail(), workflow("COMPLETE", null), null),
    4,
  );
  assert.equal(
    currentRequirementStep(
      detail(),
      workflow("COMPLETE", null),
      accepted({ accepted: false }),
    ),
    4,
  );
});
test("only current accepted materials from this flow unlock delivery", () => {
  const flow = workflow("COMPLETE", null);
  assert.equal(currentRequirementStep(detail(), flow, accepted()), 5);
  assert.equal(
    currentRequirementStep(detail(), flow, accepted({ context_valid: false })),
    4,
  );
  assert.equal(
    currentRequirementStep(detail(), flow, accepted({ flow_id: "old-flow" })),
    4,
  );
});
