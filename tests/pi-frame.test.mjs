import { test } from 'node:test';
import assert from 'node:assert/strict';
import { rgbToPng, parseFrame } from '../pi/frame.mjs';

test('encodes RGB frame as valid PNG', () => {
  const png = rgbToPng(Buffer.from([255, 0, 0, 0, 255, 0]), 2, 1);
  assert.equal(png.subarray(0, 8).toString('hex'), '89504e470d0a1a0a');
  assert.equal(png.readUInt32BE(16), 2);
  assert.equal(png.readUInt32BE(20), 1);
});
test('rejects malformed frame size and untrusted frame path', () => {
  assert.throws(() => rgbToPng(Buffer.alloc(2), 1, 1));
  assert.equal(parseFrame('F file /tmp/x.rgb 1 20 20 0 1'), null);
  assert.deepEqual(parseFrame('F file /tmp/doomscroll-abc/1.rgb 1 20 20 0 1'), { path: '/tmp/doomscroll-abc/1.rgb', width: 20, height: 20 });
});
