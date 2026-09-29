import type { ReactNode } from "react";
import { Button } from "./ui";

export function Dialog({ title, children, onClose, actions }: { title: string; children: ReactNode; onClose: () => void; actions: ReactNode }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4" role="dialog" aria-modal="true" aria-label={title}>
      <div className="w-full max-w-md rounded-lg border border-zinc-700 bg-zinc-900 p-4 shadow-xl">
        <h3 className="mb-2 text-base font-semibold">{title}</h3>
        <div className="mb-4 text-sm text-zinc-300">{children}</div>
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          {actions}
        </div>
      </div>
    </div>
  );
}
