/**
 * Live progress from the engine.
 *
 * The engine coalesces progress events and sends at most ten a second, so
 * this hook can render every event it receives without throttling again.
 *
 * The socket reconnects on its own: a long scan must not lose its progress
 * rail because the connection blipped.
 */

import { useEffect, useRef, useState } from 'react';

import { apiBase } from '../api/client';
import type { ProgressEvent } from '../api/types';

const RECONNECT_DELAY_MS = 1000;

function socketUrl(base: string): string {
  // The base is empty in production (same origin), so fall back to the page.
  const origin = base || window.location.origin;
  return `${origin.replace(/^http/, 'ws')}/api/ws/progress`;
}

export interface ProgressState {
  /** The most recent event, or null when nothing is running. */
  event: ProgressEvent | null;
  connected: boolean;
}

export function useProgress(onEvent?: (event: ProgressEvent) => void): ProgressState {
  const [event, setEvent] = useState<ProgressEvent | null>(null);
  const [connected, setConnected] = useState(false);
  // Kept in a ref so a changing callback does not tear down the socket, and
  // assigned in an effect rather than during render.
  const callback = useRef(onEvent);
  useEffect(() => {
    callback.current = onEvent;
  }, [onEvent]);

  useEffect(() => {
    let socket: WebSocket | null = null;
    let retry: number | undefined;
    let closed = false;

    const connect = async (): Promise<void> => {
      if (closed) return;

      try {
        socket = new WebSocket(socketUrl(await apiBase()));
      } catch {
        retry = window.setTimeout(() => void connect(), RECONNECT_DELAY_MS);
        return;
      }

      socket.onopen = () => setConnected(true);

      socket.onmessage = (message) => {
        try {
          const payload = JSON.parse(message.data as string) as ProgressEvent;
          setEvent(payload);
          callback.current?.(payload);
        } catch {
          // A malformed frame is not worth breaking the rail over.
        }
      };

      socket.onclose = () => {
        setConnected(false);
        if (!closed) retry = window.setTimeout(() => void connect(), RECONNECT_DELAY_MS);
      };

      socket.onerror = () => socket?.close();
    };

    void connect();

    return () => {
      closed = true;
      window.clearTimeout(retry);
      socket?.close();
    };
  }, []);

  return { event, connected };
}

/** "about 2 min left" — never a bare number of seconds. */
export function formatEta(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return '';
  if (seconds < 5) return 'almost done';
  if (seconds < 60) return `about ${Math.round(seconds)} s left`;

  const minutes = Math.round(seconds / 60);
  return `about ${minutes} min left`;
}
