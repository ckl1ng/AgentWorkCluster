import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { executeExecutorRun } from '../src/executor.js';

function tmpWorkspace() {
  return fs.mkdtemp(path.join(os.tmpdir(), 'awc-exec-'));
}

test('executeExecutorRun dispatches codex runs to the codex adapter', async () => {
  const root = await tmpWorkspace();
  const events = [];
  const emit = (type, payload) => { events.push({ type, payload }); };
  const bin = process.execPath;
  const buildArgs = (prompt) => ['-e', 'process.stdout.write("fixed text OK"); process.exit(0)'];
  const run = { prompt: 'do something', executor: 'codex' };
  const result = await executeExecutorRun({ run, workspace: { root }, emit, codex: { bin, buildArgs } });
  assert.equal(result.exit_code, 0);
  assert.match(result.sanitized_content, /fixed text OK/);
  assert.ok(events.some((e) => e.type === 'agent.run.started'));
  assert.ok(events.some((e) => e.type === 'agent.message.delta'));
  assert.ok(events.some((e) => e.type === 'agent.run.completed'));
});

test('executeCodexRun sanitizes workspace paths out of the stream', async () => {
  const root = await tmpWorkspace();
  await fs.mkdir(path.join(root, 'sub'));
  const events = [];
  const emit = (type, payload) => { events.push({ type, payload }); };
  const bin = process.execPath;
  const buildArgs = (prompt) => ['-e', 'process.stdout.write("leak " + process.cwd() + " end")'];
  const result = await executeExecutorRun({ run: { prompt: 'p', executor: 'codex' }, workspace: { root }, emit, codex: { bin, buildArgs } });
  assert.doesNotMatch(result.sanitized_content, new RegExp(root.replace(/[/\\]/g, '\\$&')));
  assert.match(result.sanitized_content, /<workspace>/);
  assert.ok(events.some((e) => e.type === 'agent.message.delta'));
});

test('executeCodexRun fails when the process exits with an error and no output', async () => {
  const root = await tmpWorkspace();
  const events = [];
  const emit = (type, payload) => { events.push({ type, payload }); };
  const bin = process.execPath;
  const buildArgs = (prompt) => ['-e', 'process.exit(2)'];
  const result = await executeExecutorRun({ run: { prompt: 'p', executor: 'codex' }, workspace: { root }, emit, codex: { bin, buildArgs } });
  assert.equal(result.exit_code, 2);
  assert.match(result.error, /code 2/);
  assert.ok(events.some((e) => e.type === 'agent.run.failed'));
});

test('executeExecutorRun rejects unknown executors', async () => {
  const root = await tmpWorkspace();
  await assert.rejects(
    executeExecutorRun({ run: { prompt: 'p', executor: 'nope' }, workspace: { root }, emit: () => {} }),
    /unknown executor/
  );
});
