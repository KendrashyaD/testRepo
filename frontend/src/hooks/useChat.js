import { useCallback, useReducer, useRef } from 'react';
import { cancelRun, deleteThread, streamChat } from '../api/chatStream';

const uid = () => Math.random().toString(36).slice(2) + Date.now().toString(36);
const initialState = { threadId: null, messages: [], running: false, status: null, plan: null };

/** Assistant messages hold ordered `parts` (text chunks and tool calls) plus generated `files`. */
function updateLastAssistant(state, fn) {
  const messages = state.messages.slice();
  const i = messages.length - 1;
  if (i < 0 || messages[i].role !== 'assistant') return state;
  messages[i] = fn(messages[i]);
  return { ...state, messages };
}

function reducer(state, action) {
  switch (action.type) {
    case 'send':
      return {
        ...state,
        running: true,
        status: null,
        messages: [
          ...state.messages,
          { id: uid(), role: 'user', parts: [{ type: 'text', text: action.text }] },
          { id: uid(), role: 'assistant', parts: [], files: [], status: 'streaming' },
        ],
      };
    case 'meta':
      return { ...state, threadId: action.threadId };
    case 'status':
      return { ...state, status: action.text };
    case 'plan':
      return { ...state, plan: action.text };
    case 'token':
      return updateLastAssistant({ ...state, status: null }, (m) => {
        const parts = m.parts.slice();
        const last = parts[parts.length - 1];
        if (last?.type === 'text') parts[parts.length - 1] = { ...last, text: last.text + action.text };
        else parts.push({ type: 'text', text: action.text });
        return { ...m, parts };
      });
    case 'tool_start':
      return updateLastAssistant({ ...state, status: null }, (m) => ({
        ...m,
        parts: [...m.parts, { type: 'tool', id: action.id, name: action.name, args: action.args, status: 'running' }],
      }));
    case 'tool_end':
      return updateLastAssistant(state, (m) => ({
        ...m,
        parts: m.parts.map((p) =>
          p.type === 'tool' && p.id === action.id ? { ...p, status: action.status, output: action.output } : p,
        ),
      }));
    case 'file':
      return updateLastAssistant(state, (m) => ({ ...m, files: [...(m.files || []), action.file] }));
    case 'error':
      return updateLastAssistant(state, (m) => ({ ...m, status: 'error', error: action.message }));
    case 'finish':
      return updateLastAssistant({ ...state, running: false, status: null }, (m) => ({
        ...m,
        status: m.status === 'streaming' ? action.final : m.status,
        parts: m.parts.map((p) =>
          p.type === 'tool' && p.status === 'running' ? { ...p, status: 'cancelled' } : p,
        ),
      }));
    case 'reset':
      return initialState;
    default:
      return state;
  }
}

export function useChat() {
  const [state, dispatch] = useReducer(reducer, initialState);
  const abortRef = useRef(null);
  const threadRef = useRef(null);

  const send = useCallback(async (text) => {
    const message = text.trim();
    if (!message || abortRef.current) return;
    const controller = new AbortController();
    abortRef.current = controller;
    dispatch({ type: 'send', text: message });

    let final = 'done';
    try {
      await streamChat({
        message,
        threadId: threadRef.current,
        signal: controller.signal,
        onEvent: ({ event, data }) => {
          switch (event) {
            case 'meta':
              threadRef.current = data.thread_id;
              dispatch({ type: 'meta', threadId: data.thread_id });
              break;
            case 'status':
              dispatch({ type: 'status', text: data.message });
              break;
            case 'token':
              dispatch({ type: 'token', text: data.text });
              break;
            case 'tool_start':
              dispatch({ type: 'tool_start', id: data.id, name: data.name, args: data.args });
              break;
            case 'tool_end':
              dispatch({ type: 'tool_end', id: data.id, status: data.status, output: data.output });
              break;
            case 'plan':
              dispatch({ type: 'plan', text: data.text });
              break;
            case 'file':
              dispatch({ type: 'file', file: data });
              break;
            case 'error':
              final = 'error';
              dispatch({ type: 'error', message: data.message });
              break;
            case 'done':
              if (data.reason === 'cancelled') final = 'cancelled';
              break;
            default:
          }
        },
      });
    } catch (err) {
      if (err.name === 'AbortError') final = 'cancelled';
      else {
        final = 'error';
        dispatch({ type: 'error', message: err.message });
      }
    } finally {
      abortRef.current = null;
      dispatch({ type: 'finish', final });
    }
  }, []);

  const stop = useCallback(() => {
    if (threadRef.current) cancelRun(threadRef.current);
    abortRef.current?.abort();
  }, []);

  const reset = useCallback(() => {
    stop();
    if (threadRef.current) deleteThread(threadRef.current);
    threadRef.current = null;
    dispatch({ type: 'reset' });
  }, [stop]);

  return { ...state, send, stop, reset };
}
