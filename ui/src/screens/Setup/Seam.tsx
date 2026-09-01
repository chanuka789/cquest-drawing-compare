import { useEffect, useRef, useState } from 'react';

/** Matches --duration-seam-pulse in tokens.css, plus a little slack. */
const PULSE_MS = 700;

interface SeamProps {
  /** Changes each time the pulse should run. */
  pulseToken: number;
}

/**
 * The comparison axis.
 *
 * A thin brand-red line with a soft glow, standing between the two issue
 * panels. It carries through the whole application: it becomes the swipe
 * divider in the lightbox and the split marker in the change list.
 *
 * It pulses once, top to bottom, when the second folder is chosen. That is
 * the only animation in the application that the user did not trigger by
 * clicking.
 *
 * The class is cleared on a timer rather than on `animationend`, because
 * under `prefers-reduced-motion` there is no animation and therefore no event
 * — and the class would stay on forever, so the pulse could never run again.
 */
export function Seam({ pulseToken }: SeamProps) {
  const [pulsing, setPulsing] = useState(false);
  const seenToken = useRef(pulseToken);

  useEffect(() => {
    if (pulseToken === seenToken.current) return;
    seenToken.current = pulseToken;

    setPulsing(true);
    const timer = window.setTimeout(() => setPulsing(false), PULSE_MS);
    return () => window.clearTimeout(timer);
  }, [pulseToken]);

  return (
    <div className={`seam${pulsing ? ' seam--pulsing' : ''}`} aria-hidden="true">
      <span className="seam__pulse" />
    </div>
  );
}
