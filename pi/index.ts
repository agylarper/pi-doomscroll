import type { ExtensionAPI } from '@earendil-works/pi-coding-agent';
import { Image, getCapabilities } from '@earendil-works/pi-tui';
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { request } from 'node:http';
import { parseFrame, rgbToPng } from './frame.mjs';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const socket = `/tmp/doomscroll-pi-${process.pid}.sock`;
type State = { status?: string; message?: string; item?: { author?: string; caption?: string }; muted?: boolean };

export default function (pi: ExtensionAPI) {
  let child: ChildProcessWithoutNullStreams | undefined;
  let state: State = {};
  let frame: { path: string; width: number; height: number } | null = null;
  let image: string | null = null;
  let refresh: (() => void) | undefined;
  let working = false;
  let held = false;
  let paused = false;
  let closing = false;
  let buffer = '';
  let decoding = false;
  let lastFrame = 0;

  function command(op: string, extra: Record<string, unknown> = {}): Promise<any> {
    return new Promise((resolve, reject) => {
      const req = request({ socketPath: socket, path: '/cmd', method: 'POST', headers: { 'content-type': 'application/json' } }, res => {
        let data = '';
        res.setEncoding('utf8');
        res.on('data', chunk => data += chunk);
        res.on('end', () => {
          try { const result = JSON.parse(data); if (res.statusCode !== 200) reject(new Error(result.error || data)); else resolve(result); }
          catch (error) { reject(error); }
        });
      });
      req.on('error', reject);
      req.end(JSON.stringify({ op, ...extra }));
    });
  }
  function send(op: string, extra?: Record<string, unknown>) { void command(op, extra).catch(() => {}); }
  function playback() { send(!paused && (working || held) && !!refresh ? 'play' : 'pause'); }
  async function decode() {
    if (decoding || !frame || !refresh) return;
    decoding = true;
    try {
      const current = frame;
      frame = null;
      const raw = await readFile(current.path);
      image = rgbToPng(raw, current.width, current.height).toString('base64');
      refresh?.();
    } catch { /* rotating frame may have been replaced; use the next one */ }
    finally { decoding = false; if (frame) void decode(); }
  }
  function start() {
    if (child || closing) return;
    child = spawn('python3', [join(root, 'bin/doomscrolld.py'), '--sock', socket], {
      env: { ...process.env, DOOMSCROLL_TRANSPORT: 'file' }, stdio: ['ignore', 'pipe', 'pipe'],
    });
    child.stdout.on('data', (data: Buffer) => {
      buffer += data.toString();
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';
      for (const line of lines) {
        if (line.startsWith('F ')) {
          // Drop intermediate frames if Pi cannot render at video speed.
          if (Date.now() - lastFrame > 90) {
            frame = parseFrame(line);
            lastFrame = Date.now();
            void decode();
          }
        } else if (line.startsWith('S ')) {
          try { state = JSON.parse(line.slice(2)); refresh?.(); } catch { /* ignore malformed state */ }
        } else if (line.startsWith('R ')) playback();
      }
    });
    child.on('exit', () => { child = undefined; if (!closing) { state = { message: 'Player stopped. Try /doomscroll again.' }; refresh?.(); } });
    child.on('error', error => { state = { message: `Player error: ${error.message}` }; refresh?.(); });
  }
  pi.registerCommand('doomscroll', {
    description: 'Guest video viewer (default), optional personal/login/logout, or overlay',
    handler: async (args, ctx) => {
      const mode = args?.trim();
      if (mode === 'login' || mode === 'logout') {
        ctx.ui.notify(mode === 'login' ? 'Login langsung di browser TikTok, lalu tutup browser untuk menyimpan sesi. Guest tetap default.' : 'Menghapus sesi TikTok lokal…', 'info');
        await new Promise<void>(resolve => {
          const browser = spawn('python3', [join(root, 'bin/personal.py'), mode], { stdio: 'ignore' });
          browser.on('error', () => { ctx.ui.notify('Gagal menjalankan browser TikTok. Lihat setup Playwright di README.', 'error'); resolve(); });
          browser.on('exit', code => {
            ctx.ui.notify(code === 0 ? 'Selesai. Guest tetap default; /doomscroll personal untuk opsi akun.' : 'Gagal. Pastikan Playwright terpasang dan viewer personal ditutup. Coba npm run ' + mode + ' untuk petunjuk.', code === 0 ? 'info' : 'error');
            resolve();
          });
        });
        return;
      }
      if (mode === 'personal') {
        ctx.ui.notify(`Mode akun opsional: buka split Ghostty, lalu node ${join(root, 'pi/viewer.mjs')} --personal. Login dulu dengan /doomscroll login.`, 'info');
        return;
      }
      if (mode !== 'overlay') {
        ctx.ui.notify(`Split Ghostty ke kanan (Ctrl+Shift+O), lalu jalankan: node ${join(root, 'pi/viewer.mjs')}. Pi tetap di kiri. /doomscroll overlay untuk mode lama.`, 'info');
        return;
      }
      if (ctx.mode !== 'tui') { ctx.ui.notify('Doomscroll needs interactive Pi in Ghostty or kitty.', 'warning'); return; }
      if (getCapabilities().images !== 'kitty') { ctx.ui.notify('Doomscroll needs kitty graphics (Ghostty or kitty).', 'warning'); return; }
      start();
      held = true;
      paused = false;
      await ctx.ui.custom((tui, theme, _keys, done) => {
        let active = true;
        refresh = () => { if (active) tui.requestRender(); };
        playback();
        return {
          render(width: number) {
            const title = theme.fg('accent', 'Doomscroll  j/k: next/prev  p: pause  m: mute  o: browser  q: close');
            const label = state.item ? `${state.item.author || ''}  ${state.item.caption || ''}` : (state.message || state.status || 'Loading feed…');
            const lines = [title, label.slice(0, Math.max(1, width - 2))];
            if (image) lines.push(...new Image(image, 'image/png', { fallbackColor: s => theme.fg('muted', s) }, { maxWidthCells: Math.min(55, width - 2), maxHeightCells: 28 }).render(width));
            return lines;
          },
          handleInput(data: string) {
            if (data === 'q' || data === '\x1b') { active = false; refresh = undefined; held = false; playback(); done(undefined); }
            else if (data === 'j') { paused = false; send('next'); }
            else if (data === 'k') { paused = false; send('prev'); }
            else if (data === 'p') { paused = !paused; playback(); }
            else if (data === 'm') send('mute', { muted: !state.muted });
            else if (data === 'o') send('open');
          },
          invalidate() {},
        };
      }, { overlay: true, overlayOptions: { width: '65%', maxHeight: '90%', anchor: 'right-center' } });
    },
  });
  pi.on('agent_start', () => { working = true; playback(); });
  pi.on('agent_settled', () => { working = false; playback(); });
  pi.on('session_shutdown', () => { closing = true; child?.kill('SIGTERM'); child = undefined; });
}
