import { useEffect, useRef, useState } from 'react';

export default function Composer({ running, onSend, onStop }) {
  const [text, setText] = useState('');
  const ref = useRef(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [text]);

  const submit = () => {
    if (!text.trim() || running) return;
    onSend(text);
    setText('');
  };

  return (
    <div className="flex items-end gap-2 rounded-lg border border-slate-300 bg-white p-2 focus-within:border-slate-500">
      <textarea
        ref={ref}
        rows={1}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            submit();
          }
        }}
        placeholder="Tell the agent what to do on a website…  (Shift+Enter for a new line)"
        className="max-h-[200px] flex-1 resize-none bg-transparent px-2 py-1.5 text-sm outline-none placeholder:text-slate-400"
      />
      {running ? (
        <button type="button" onClick={onStop} className="rounded-md bg-red-600 px-4 py-2 text-sm font-medium text-white hover:bg-red-700">
          Stop
        </button>
      ) : (
        <button
          type="button"
          onClick={submit}
          disabled={!text.trim()}
          className="rounded-md bg-slate-800 px-4 py-2 text-sm font-medium text-white hover:bg-slate-900 disabled:cursor-not-allowed disabled:opacity-40"
        >
          Send
        </button>
      )}
    </div>
  );
}
