import { test } from 'node:test';
import assert from 'node:assert/strict';
import { inflateSync } from 'node:zlib';
import { randomFillSync } from 'node:crypto';
import * as frames from '../pi/frame.mjs';

function packets(encoded) {
  return [...encoded.matchAll(/\x1b_G([^;]+);([^\x1b]*)\x1b\\/g)];
}

test('Kitty RGB transmission round-trips pixels without PNG encoding', async () => {
  assert.equal(typeof frames.rgbToKitty, 'function');
  const rgb = Buffer.from([255, 0, 0, 0, 255, 0]);
  const encoded = await frames.rgbToKitty(rgb, 2, 1, 40, 27);
  const parts = packets(encoded);
  const header = Object.fromEntries(parts[0][1].split(',').map(pair => pair.split('=')));
  assert.equal(header.f, '24');
  assert.equal(header.o, 'z');
  assert.equal(header.s, '2');
  assert.equal(header.v, '1');
  assert.equal(header.c, '40');
  assert.equal(header.r, '27');
  assert.equal(header.a, 'T');
  assert.equal(header.C, '1');
  assert.equal(header.q, '2');
  // Reuse an image ID so old video frames don't accumulate in terminal memory.
  assert.equal(header.i, '1');
  assert.equal(header.m, '0');
  assert.deepEqual(inflateSync(Buffer.from(parts.map(p => p[2]).join(''), 'base64')), rgb);
});

test('large RGB frames use complete, bounded Kitty chunks', async () => {
  assert.equal(typeof frames.rgbToKitty, 'function');
  const rgb = randomFillSync(Buffer.alloc(128 * 128 * 3));
  const parts = packets(await frames.rgbToKitty(rgb, 128, 128, 40, 27));
  assert.ok(parts.length > 1);
  for (let i = 0; i < parts.length; i++) {
    assert.ok(parts[i][2].length <= 4096);
    assert.equal(parts[i][2].length % 4, 0);
    const more = i < parts.length - 1 ? 1 : 0;
    if (i > 0) assert.equal(parts[i][1], `m=${more}`);
    else assert.ok(parts[i][1].endsWith(`m=${more}`));
  }
  assert.deepEqual(inflateSync(Buffer.from(parts.map(p => p[2]).join(''), 'base64')), rgb);
});

test('Kitty encoder rejects invalid RGB data before transmission', async () => {
  assert.equal(typeof frames.rgbToKitty, 'function');
  await assert.rejects(frames.rgbToKitty(Buffer.alloc(2), 1, 1, 40, 27), /Invalid RGB frame/);
  await assert.rejects(frames.rgbToKitty(Buffer.alloc(0), 0, 1, 40, 27), /Invalid RGB frame/);
});
