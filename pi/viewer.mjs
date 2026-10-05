#!/usr/bin/env node
import { spawn } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { request } from 'node:http';
import { parseFrame, rgbToKitty } from './frame.mjs';
import { createFrameWriter } from './frame-writer.mjs';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const args = process.argv.slice(2);
if (args.some(arg => arg !== '--personal')) {
  console.error('Usage: node pi/viewer.mjs [--personal] (guest is default)');
  process.exit(1);
}
const socket = `/tmp/doomscroll-viewer-${process.pid}.sock`;
// Keep pipx's newer yt-dlp first. The player selects system ffmpeg for audio.
const playerPath = [`${process.env.HOME}/.local/bin`, process.env.PATH || ''].join(':');
const player = spawn('python3', [join(root, 'bin/doomscrolld.py'), '--sock', socket, ...args], {
  env: { ...process.env, PATH: playerPath, DOOMSCROLL_TRANSPORT: 'file' },
  stdio: ['ignore', 'pipe', 'pipe'],
});
let state = {};
let buffer = '';
let latest = null;
let drawing = false;
let closed = false;
const writer = createFrameWriter(process.stdout);

function cmd(op, extra = {}) {
  return new Promise((resolve, reject) => {
    const req = request({ socketPath: socket, path: '/cmd', method: 'POST', headers: { 'content-type': 'application/json' } }, res => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => { try { resolve(JSON.parse(data)); } catch { resolve(null); } });
    });
    req.on('error', reject);
    req.end(JSON.stringify({ op, ...extra }));
  });
}
function send(op, extra) { void cmd(op, extra).catch(() => {}); }
function size() {
  const cols = process.stdout.columns || 40;
  const rows = process.stdout.rows || 32;
  send('size', { cols: Math.max(16, Math.min(cols - 2, 65)), rows: Math.max(12, rows - 5) });
}
function title() {
  const name = state.item?.author || state.message || state.status || 'Loading…';
  process.stdout.write(`\x1b[H\x1b[2K Doomscroll ${args.includes('--personal') ? 'Personal' : 'Guest'} | ${String(name).slice(0, 45)}\r\n\x1b[2K j/k next/prev  p pause  m mute  o browser  q close`);
}
async function draw() {
  if (drawing || !latest || closed) return;
  drawing = true;
  const current = latest;
  latest = null;
  try {
    const raw = await readFile(current.path);
    const encoded = await rgbToKitty(raw, current.width, current.height,
      Math.max(1, Math.min(process.stdout.columns || 40, 65)),
      Math.max(1, (process.stdout.rows || 32) - 5));
    // Transmit one complete frame at a time; replace queued frames if stdout lags.
    writer.push('\x1b[3;1H' + encoded);
  } catch { /* file ring may rotate before reading */ }
  finally { drawing = false; if (latest) void draw(); }
}
function stop(code = 0) {
  if (closed) return;
  closed = true;
  writer.close();
  player.kill('SIGTERM');
  if (process.stdin.isTTY) process.stdin.setRawMode(false);
  process.stdout.write('\x1b_Ga=d,d=A\x1b\\\x1b[?25h\x1b[?1049l');
  process.exit(code);
}
process.on('SIGINT', () => stop());
process.on('SIGTERM', () => stop());
player.on('error', error => { console.error(error.message); stop(1); });
player.on('exit', code => { if (!closed) stop(code || 1); });
player.stderr.on('data', data => process.stderr.write(data));
player.stdout.on('data', data => {
  buffer += data.toString();
  const lines = buffer.split('\n');
  buffer = lines.pop() || '';
  for (const line of lines) {
    if (line.startsWith('R ')) { size(); send('play'); }
    else if (line.startsWith('S ')) { try { state = JSON.parse(line.slice(2)); title(); } catch {} }
    else if (line.startsWith('E ')) { process.stderr.write(line.slice(2) + '\n'); }
    else if (line.startsWith('F ')) {
      latest = parseFrame(line);
      void draw();
    }
  }
});
if (!process.stdin.isTTY || !process.stdout.isTTY) { console.error('Run this viewer in a Ghostty split, not a pipe.'); stop(1); }
else {
  process.stdout.write('\x1b[?1049h\x1b[?25l\x1b[2J');
  process.stdin.setRawMode(true);
  process.stdin.resume();
  process.stdin.on('data', data => {
    const key = data.toString();
    if (key === 'q' || key === '\x03' || key === '\x1b') stop();
    else if (key === 'j') send('next');
    else if (key === 'k') send('prev');
    else if (key === 'p') send(state.status === 'playing' ? 'pause' : 'play');
    else if (key === 'm') send('mute', { muted: !state.muted });
    else if (key === 'o') send('open');
  });
  process.stdout.on('resize', size);
}
