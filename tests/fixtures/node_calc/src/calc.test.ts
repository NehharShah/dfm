import assert from "node:assert/strict";
import { test } from "node:test";
import { add } from "./calc.ts";

test("adds two numbers", () => {
  assert.equal(add(2, 3), 5);
});

test("handles zero", () => {
  assert.equal(add(0, 0), 0);
});
