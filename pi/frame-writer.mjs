// Keep at most one pending frame. stdout's callback signals that its previous
// frame has been handed off; if rendering falls behind, discard stale frames.
export function createFrameWriter(output) {
  let busy = false;
  let pending = null;
  let closed = false;
  function flush() {
    if (busy || closed || pending === null) return;
    const frame = pending;
    pending = null;
    busy = true;
    output.write(frame, () => {
      busy = false;
      flush();
    });
  }
  return {
    push(frame) { if (!closed) { pending = frame; flush(); } },
    close() { closed = true; pending = null; },
  };
}
