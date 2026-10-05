import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createFrameWriter } from '../pi/frame-writer.mjs';

function output() {
  const callbacks = [];
  const writes = [];
  return { writes, callbacks, write(data, callback) { writes.push(data); callbacks.push(callback); return true; } };
}
test('only latest frame is queued while terminal is still writing', async () => {
  const out = output();
  const writer = createFrameWriter(out);
  writer.push('one');
  writer.push('two');
  writer.push('three');
  assert.deepEqual(out.writes, ['one']);
  out.callbacks.shift()();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(out.writes, ['one', 'three']);
});
test('writer does not send another frame after close', async () => {
  const out = output();
  const writer = createFrameWriter(out);
  writer.push('one'); writer.push('two'); writer.close();
  out.callbacks.shift()();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(out.writes, ['one']);
});
