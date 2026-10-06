import { useEffect, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Button } from "./ui";

export function Dialog({ title, children, onClose, actions }: { title: string; children: ReactNode; onClose: () => void; actions: ReactNode }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  // Portal: an ancestor with backdrop-filter (the sticky header) would otherwise trap position: fixed.
  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" role="dialog" aria-modal="true" aria-label={title}>
      <div className="brackets w-full max-w-md rounded-sm border border-zinc-700 bg-zinc-900 shadow-[0_24px_64px_-12px_rgb(0_0_0/0.8),0_0_0_1px_rgb(47_224_245/0.08)]">
        <header className="flex items-center gap-2 border-b border-zinc-800 bg-zinc-950/70 px-4 py-2">
          <span aria-hidden className="label rounded-[1px] bg-amber-400 px-1 text-[0.625rem] text-zinc-950">Confirm</span>
          <h3 className="font-mono text-sm font-medium text-zinc-50">{title}</h3>
        </header>
        <div className="px-4 py-3 text-sm leading-relaxed text-zinc-300">{children}</div>
        <div className="flex justify-end gap-2 border-t border-zinc-800 px-4 py-2.5">
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          {actions}
        </div>
      </div>
    </div>,
    document.body,
  );
}
