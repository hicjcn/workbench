import test from 'node:test';
import assert from 'node:assert/strict';
import { greeting } from './server.mjs';

test('default greeting', () => {
  assert.deepEqual(greeting(), { message: 'hello world' });
});

test('named greeting', () => {
  assert.deepEqual(greeting('developer'), { message: 'hello developer' });
});

