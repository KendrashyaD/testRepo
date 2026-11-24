/**
 * Streams a chat turn over plain HTTP: POST + ReadableStream (SSE wire format).
 * EventSource is GET-only, so we parse the event stream ourselves.
 */
export async function streamChat({ message, threadId, signal, onEvent }) {
  const res = await fetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify({ message, thread_id: threadId }),
    signal,
  });

  if (!res.ok || !res.body) {
    const detail = await res.text().catch(() => '');
    throw new Error(`Request failed (${res.status}) ${detail}`.trim());
  }

  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value;
    let idx;
    while ((idx = buffer.indexOf('\n\n')) !== -1) {
      const raw = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      const evt = parseEvent(raw);
      if (evt) onEvent(evt);
    }
  }
}

function parseEvent(raw) {
  let event = 'message';
  const data = [];
  for (const line of raw.split('\n')) {
    if (!line || line.startsWith(':')) continue; // comments = heartbeats
    const i = line.indexOf(':');
    const field = i === -1 ? line : line.slice(0, i);
    let val = i === -1 ? '' : line.slice(i + 1);
    if (val.startsWith(' ')) val = val.slice(1);
    if (field === 'event') event = val;
    else if (field === 'data') data.push(val);
  }
  if (!data.length) return null;
  const text = data.join('\n');
  try {
    return { event, data: JSON.parse(text) };
  } catch {
    return { event, data: text };
  }
}

export const cancelRun = (threadId) =>
  fetch(`/api/chat/${threadId}/cancel`, { method: 'POST' }).catch(() => {});

export const deleteThread = (threadId) =>
  fetch(`/api/threads/${threadId}`, { method: 'DELETE' }).catch(() => {});

export const getInfo = () =>
  fetch('/api/info').then((r) => (r.ok ? r.json() : null)).catch(() => null);
