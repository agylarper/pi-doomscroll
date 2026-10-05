import { deflate, deflateSync } from 'node:zlib';
import { promisify } from 'node:util';

const compress = promisify(deflate);

const crcTable = Array.from({ length: 256 }, (_, n) => {
  let c = n;
  for (let i = 0; i < 8; i++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  return c >>> 0;
});
function chunk(type, data) {
  const size = Buffer.alloc(4);
  size.writeUInt32BE(data.length);
  const body = Buffer.concat([Buffer.from(type), data]);
  let crc = 0xffffffff;
  for (const byte of body) crc = crcTable[(crc ^ byte) & 255] ^ (crc >>> 8);
  const checksum = Buffer.alloc(4);
  checksum.writeUInt32BE((crc ^ 0xffffffff) >>> 0);
  return Buffer.concat([size, body, checksum]);
}
function validateRgb(rgb, width, height) {
  if (!Number.isSafeInteger(width) || !Number.isSafeInteger(height) || width < 1 || height < 1 || width > 4096 || height > 4096 || rgb.length !== width * height * 3) throw new Error('Invalid RGB frame');
}

// Compress on zlib's worker pool instead of blocking input and frame intake.
// Raw RGB also avoids PNG scanline copies and the JavaScript CRC pass.
export async function rgbToKitty(rgb, width, height, cols, rows) {
  validateRgb(rgb, width, height);
  const payload = (await compress(rgb, { level: 1 })).toString('base64');
  const chunks = [];
  for (let i = 0; i < payload.length; i += 4096) {
    const more = i + 4096 < payload.length ? 1 : 0;
    const header = i === 0
      ? `a=T,f=24,t=d,o=z,s=${width},v=${height},i=1,q=2,C=1,c=${cols},r=${rows},m=${more}`
      : `m=${more}`;
    chunks.push(`\x1b_G${header};${payload.slice(i, i + 4096)}\x1b\\`);
  }
  return chunks.join('');
}

export function rgbToPng(rgb, width, height) {
  validateRgb(rgb, width, height);
  const scanlines = Buffer.alloc(height * (width * 3 + 1));
  for (let y = 0; y < height; y++) rgb.copy(scanlines, y * (width * 3 + 1) + 1, y * width * 3, (y + 1) * width * 3);
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width, 0);
  header.writeUInt32BE(height, 4);
  header[8] = 8;
  header[9] = 2;
  return Buffer.concat([Buffer.from('89504e470d0a1a0a', 'hex'), chunk('IHDR', header), chunk('IDAT', deflateSync(scanlines, { level: 1 })), chunk('IEND', Buffer.alloc(0))]);
}
export function parseFrame(line) {
  const parts = line.split(' ');
  if (parts.length !== 8 || parts[0] !== 'F' || parts[1] !== 'file' || !/^\/tmp\/doomscroll-[^/]+\/(?:\d+|poster)\.rgb$/.test(parts[2])) return null;
  const width = Number(parts[4]);
  const height = Number(parts[5]);
  if (!Number.isInteger(width) || !Number.isInteger(height) || width < 1 || height < 1 || width > 4096 || height > 4096) return null;
  return { path: parts[2], width, height };
}
