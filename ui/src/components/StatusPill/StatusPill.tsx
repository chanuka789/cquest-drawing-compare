import type { ReactNode } from 'react';

import './StatusPill.css';

export type StatusTone = 'neutral' | 'ok' | 'warn' | 'info' | 'danger';

interface StatusPillProps {
  tone?: StatusTone;
  children: ReactNode;
  title?: string;
}

/** A small labelled state marker. Sentence case, never all-caps. */
export function StatusPill({ tone = 'neutral', children, title }: StatusPillProps) {
  return (
    <span className={`pill pill--${tone}`} title={title}>
      <span className="pill__dot" aria-hidden="true" />
      {children}
    </span>
  );
}
