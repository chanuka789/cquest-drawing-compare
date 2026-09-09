/**
 * One pickable sheet pane of the Manual alignment screen (Task 4.13).
 *
 * Each pane is an independent tiled viewer with its own pan and zoom, so the
 * user can bring the same detail of both sheets into view side by side. The
 * rendering reuses the Lightbox kit's pure helpers (`visibleTiles`,
 * `tileSpans`) with the same Canvas 2D drawing pattern the Lightbox falls
 * back to: a device-pixel-ratio-aware `<canvas>`, an always-running rAF
 * draw, drag-to-pan, wheel-zoom anchored at the pointer, fit on first paint
 * of a sheet, and subtle placeholders for tiles still loading.
 *
 * A click (a pointer press that never turned into a drag) is converted from
 * screen pixels back through the current view transform into SHEET pixels
 * and reported to the parent, which owns the workflow (markers, pairing).
 * Markers arrive as props in sheet coordinates and are drawn on the canvas
 * each frame, numbered so old and new panes line up by number.
 *
 * Marker colours come from the status tokens, read through the same
 * `getComputedStyle` pattern the Lightbox uses — no hard-coded colours.
 * Brand red never appears here.
 */

import { useEffect, useRef, useState } from 'react';

import { tileSpans, useTileLoader, visibleTiles } from '../../components/Lightbox';
import type { TileManifest } from '../../api/types';

/** Zoom range, in screen pixels per sheet pixel. */
const MIN_SCALE = 0.005;
const MAX_SCALE = 64;
/** Wheel zoom rate: one click (deltaY ≈ ±100) zooms about 15 %. */
const WHEEL_ZOOM_RATE = 0.0016;
/** Empty margin around the sheet when the pane fits it. */
const FIT_PAD_PX = 32;
/** A pointer press that moves less than this is a click, not a pan. */
const DRAG_THRESHOLD_PX = 4;
/** Marker size on screen — markers never scale with the drawing. */
const MARKER_RADIUS_PX = 10;

/** One numbered marker on a pane, in sheet pixels. */
export interface SheetMarker {
  x: number;
  y: number;
  /** 1-based pair number; the unpaired marker carries the next number. */
  number: number;
  /** True while this old-side marker is waiting for its new-side partner. */
  pending: boolean;
}

interface ManualSheetCanvasProps {
  sheetId: string | null;
  filename: string;
  markers: SheetMarker[];
  canPick: boolean;
  onPick: (x: number, y: number) => void;
}

interface ViewState {
  x: number;
  y: number;
  scale: number;
}

/** The tokens the drawing and the markers read, resolved once. */
interface PaneColors {
  room: string;
  sheet: string;
  sheetDim: string;
  textLow: string;
  textHi: string;
  marker: string[];
}

let cachedColors: PaneColors | null = null;

function paneColors(): PaneColors {
  if (cachedColors !== null) return cachedColors;
  const read = (name: string, fallback: string): string =>
    getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
  cachedColors = {
    room: read('--room-900', '#14181c'),
    sheet: read('--sheet', '#f7f6f3'),
    sheetDim: read('--sheet-dim', '#e8e6e1'),
    textLow: read('--text-low', '#66727e'),
    textHi: read('--text-hi', '#edf1f5'),
    marker: [
      read('--info', '#2e9bd6'),
      read('--ok', '#2bb673'),
      read('--warn', '#e39a00'),
      read('--danger', '#e8442a'),
    ],
  };
  return cachedColors;
}

function clampScale(scale: number): number {
  if (!Number.isFinite(scale)) return 1;
  return Math.min(MAX_SCALE, Math.max(MIN_SCALE, scale));
}

/** Everything the loop and the pointer handlers need beyond props. */
interface PaneCore {
  view: ViewState;
  width: number;
  height: number;
  dpr: number;
  /** Fit the current sheet once, and only once, per content change. */
  fitRan: boolean;
  requestedKey: string;
  rafId: number;
  destroyed: boolean;
}

export function ManualSheetCanvas(props: ManualSheetCanvasProps) {
  const { sheetId, filename, markers, canPick, onPick } = props;

  const loader = useTileLoader();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  /** Current sheet id for the loop and the pointer handlers. */
  const sheetIdRef = useRef<string | null>(null);
  const coreRef = useRef<PaneCore | null>(null);

  /** Manifest presence for the caption; the draw loop reads it sync. */
  const [manifest, setManifest] = useState<TileManifest | null>(null);
  const [manifestFailed, setManifestFailed] = useState(false);

  // Handlers change with every render; the loop and the pointer logic read
  // the latest values through refs, never through stale closures.
  const markersRef = useRef(markers);
  useEffect(() => {
    markersRef.current = markers;
  }, [markers]);
  const canPickRef = useRef(canPick);
  useEffect(() => {
    canPickRef.current = canPick;
  }, [canPick]);
  const onPickRef = useRef(onPick);
  useEffect(() => {
    onPickRef.current = onPick;
  }, [onPick]);

  /** Fit the current sheet exactly once, once its size is known. */
  const maybeFit = (core: PaneCore): void => {
    if (core.fitRan) return;
    const currentSheetId = sheetIdRef.current;
    if (!currentSheetId) return;
    const currentManifest = loader.getManifest(currentSheetId);
    if (!currentManifest || !(core.width > 0) || !(core.height > 0)) return;
    core.fitRan = true;

    const scale = clampScale(
      Math.min(
        (core.width - FIT_PAD_PX * 2) / currentManifest.width_px,
        (core.height - FIT_PAD_PX * 2) / currentManifest.height_px,
      ),
    );
    core.view.scale = scale;
    core.view.x = currentManifest.width_px / 2 - core.width / (2 * scale);
    core.view.y = currentManifest.height_px / 2 - core.height / (2 * scale);
  };

  // ── One core, created and torn down with the component ───────────────
  useEffect(() => {
    const core: PaneCore = {
      view: { x: 0, y: 0, scale: 1 },
      width: 0,
      height: 0,
      dpr: 1,
      fitRan: false,
      requestedKey: '',
      rafId: 0,
      destroyed: false,
    };
    coreRef.current = core;
    return () => {
      core.destroyed = true;
      if (core.rafId !== 0) cancelAnimationFrame(core.rafId);
      coreRef.current = null;
    };
  }, []);

  // ── The draw loop ────────────────────────────────────────────────────
  useEffect(() => {
    const canvas = canvasRef.current;
    const core = coreRef.current;
    if (!canvas || !core) return;

    const context = canvas.getContext('2d');
    if (!context) return;

    const draw = (): void => {
      if (core.destroyed) return;
      core.rafId = requestAnimationFrame(draw);
      const colors = paneColors();
      if (!(core.width > 0) || !(core.height > 0)) return;

      context.setTransform(core.dpr, 0, 0, core.dpr, 0, 0);
      context.fillStyle = colors.room;
      context.fillRect(0, 0, core.width, core.height);

      const currentSheetId = sheetIdRef.current;
      if (currentSheetId === null) return;
      const currentManifest = loader.getManifest(currentSheetId);
      if (!currentManifest || !(currentManifest.width_px > 0) || !(currentManifest.height_px > 0)) {
        return;
      }

      const view = core.view;
      context.save();
      context.transform(view.scale, 0, 0, view.scale, -view.x * view.scale, -view.y * view.scale);

      // The visible tiles, requested only when the wanted set changes.
      const visible = visibleTiles(currentSheetId, { ...view, width: core.width, height: core.height }, currentManifest, {
        devicePixelRatio: core.dpr,
      });
      const wantedKey = visible
        .map((tile) => tile.key)
        .sort()
        .join('|');
      if (wantedKey !== core.requestedKey) {
        core.requestedKey = wantedKey;
        loader.want(visible);
      }

      // The sheet backdrop, then the tiles that have arrived.
      context.fillStyle = colors.sheet;
      context.fillRect(0, 0, currentManifest.width_px, currentManifest.height_px);
      for (const tile of visible) {
        const spans = tileSpans(currentManifest, tile.level);
        if (!spans) continue;
        const image = loader.getBitmap(tile.key);
        if (image !== undefined) {
          context.drawImage(
            image,
            tile.x * spans.spanX,
            tile.y * spans.spanY,
            spans.spanX,
            spans.spanY,
          );
        } else {
          // Subtle placeholder: the sheet is there, this tile is on its way.
          context.globalAlpha = 0.55;
          context.fillStyle = colors.sheetDim;
          context.fillRect(tile.x * spans.spanX, tile.y * spans.spanY, spans.spanX, spans.spanY);
          context.globalAlpha = 1;
        }
      }
      context.restore();

      // Markers stay screen-sized whatever the zoom (viewer chrome, like the
      // Lightbox minimap — never drawing content).
      for (const marker of markersRef.current) {
        drawMarker(context, marker, view, core, colors);
      }
    };

    draw();
    return () => {
      if (core.rafId !== 0) cancelAnimationFrame(core.rafId);
      core.rafId = 0;
    };
    // The loop must not restart when props change; it reads them via refs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Sizing + fit-once-per-sheet ──────────────────────────────────────
  useEffect(() => {
    const container = containerRef.current;
    const core = coreRef.current;
    if (!container || !core) return;

    const measure = (): void => {
      const rect = container.getBoundingClientRect();
      const width = Math.round(rect.width);
      const height = Math.round(rect.height);
      const dpr = window.devicePixelRatio || 1;
      const canvas = canvasRef.current;
      if (canvas) {
        canvas.width = Math.max(1, Math.round(width * dpr));
        canvas.height = Math.max(1, Math.round(height * dpr));
        canvas.style.width = `${width}px`;
        canvas.style.height = `${height}px`;
      }
      core.width = width;
      core.height = height;
      core.dpr = dpr;
      maybeFit(core);
    };
    measure();

    let observer: ResizeObserver | null = null;
    if (typeof ResizeObserver !== 'undefined') {
      observer = new ResizeObserver(measure);
      observer.observe(container);
    }
    return () => observer?.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Manifest fetching ────────────────────────────────────────────────
  useEffect(() => {
    const core = coreRef.current;
    const previous = sheetIdRef.current;
    sheetIdRef.current = sheetId;
    if (previous !== null && previous !== sheetId) {
      loader.releaseSheet(previous);
      if (core) core.fitRan = false;
    }
    setManifest(null);
    setManifestFailed(false);
    if (!sheetId) return;

    let alive = true;
    loader.ensureManifest(sheetId).then((loaded) => {
      if (!alive) return;
      setManifest(loaded);
      if (loaded === null) {
        setManifestFailed(true);
        return;
      }
      const currentCore = coreRef.current;
      if (currentCore) maybeFit(currentCore);
    });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loader, sheetId]);

  // ── Wheel zoom, native and non-passive (React's would be passive) ────
  useEffect(() => {
    const container = containerRef.current;
    const core = coreRef.current;
    if (!container || !core) return;
    const onWheel = (event: WheelEvent): void => {
      event.preventDefault();
      const rect = container.getBoundingClientRect();
      const sx = event.clientX - rect.left;
      const sy = event.clientY - rect.top;
      const deltaY = event.deltaMode === 1 ? event.deltaY * 33 : event.deltaY;
      zoomAt(core, sx, sy, Math.exp(-deltaY * WHEEL_ZOOM_RATE));
    };
    container.addEventListener('wheel', onWheel, { passive: false });
    return () => container.removeEventListener('wheel', onWheel);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Pointer: drag to pan, press-without-drag places a marker ─────────
  const dragRef = useRef<{
    pointerId: number;
    startX: number;
    startY: number;
    moved: boolean;
  } | null>(null);

  const sheetPointFromEvent = (
    event: React.PointerEvent<HTMLDivElement>,
  ): { x: number; y: number } | null => {
    const core = coreRef.current;
    const currentSheetId = sheetIdRef.current;
    const currentManifest = currentSheetId === null ? null : loader.getManifest(currentSheetId);
    if (!core || !currentManifest) return null;
    const rect = containerRef.current?.getBoundingClientRect();
    if (!rect) return null;
    const sx = event.clientX - rect.left;
    const sy = event.clientY - rect.top;
    const x = core.view.x + sx / core.view.scale;
    const y = core.view.y + sy / core.view.scale;
    if (x < 0 || y < 0 || x > currentManifest.width_px || y > currentManifest.height_px) {
      return null;
    }
    return { x, y };
  };

  const onPointerDown = (event: React.PointerEvent<HTMLDivElement>): void => {
    if (event.pointerType === 'mouse' && event.button !== 0) return;
    const container = containerRef.current;
    if (!container || sheetIdRef.current === null) return;
    try {
      container.setPointerCapture(event.pointerId);
    } catch {
      // The pointer may already be gone; dragging still works without it.
    }
    dragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      moved: false,
    };
  };

  const onPointerMove = (event: React.PointerEvent<HTMLDivElement>): void => {
    const drag = dragRef.current;
    const core = coreRef.current;
    if (!drag || !core || drag.pointerId !== event.pointerId) return;
    const dx = event.clientX - drag.startX;
    const dy = event.clientY - drag.startY;
    if (!drag.moved && Math.hypot(dx, dy) < DRAG_THRESHOLD_PX) return;
    drag.moved = true;
    core.view.x -= dx / core.view.scale;
    core.view.y -= dy / core.view.scale;
    drag.startX = event.clientX;
    drag.startY = event.clientY;
  };

  const onPointerEnd = (event: React.PointerEvent<HTMLDivElement>): void => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    dragRef.current = null;
    if (!drag.moved && canPickRef.current) {
      const point = sheetPointFromEvent(event);
      if (point) onPickRef.current(point.x, point.y);
    }
  };

  const picking = sheetId !== null && manifest !== null && !manifestFailed && canPick;
  const caption =
    sheetId === null
      ? 'No sheet is available for this side.'
      : manifestFailed
        ? 'This sheet has no rendered tiles, so it cannot be picked.'
        : manifest === null
          ? 'Loading the sheet…'
          : filename;

  return (
    <div className="manual-pane">
      <header className="manual-pane__head micro">
        <span className="manual-pane__title" title={caption}>
          {caption}
        </span>
        {picking && (
          <span className="manual-pane__hint" aria-hidden="true">
            drag to pan · scroll to zoom · click to mark
          </span>
        )}
      </header>
      <div
        className="manual-pane__canvas-wrap"
        ref={containerRef}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerEnd}
        onPointerCancel={onPointerEnd}
        style={{ cursor: picking ? 'crosshair' : 'default' }}
      >
        <canvas ref={canvasRef} aria-label={caption} />
        {sheetId !== null && manifest === null && !manifestFailed && (
          <p className="manual-pane__overlay micro">Loading tiles…</p>
        )}
        {manifestFailed && (
          <p className="manual-pane__overlay micro" role="alert">
            This sheet has no rendered tiles, so it cannot be picked.
          </p>
        )}
        {sheetId === null && (
          <p className="manual-pane__overlay micro">No sheet is available for this side.</p>
        )}
      </div>
    </div>
  );
}

/** Zoom around a screen point, keeping the world point under it fixed. */
function zoomAt(core: PaneCore, sx: number, sy: number, factor: number): void {
  const view = core.view;
  if (!(view.scale > 0) || !(core.width > 0) || !(core.height > 0)) return;
  const newScale = clampScale(view.scale * factor);
  if (newScale === view.scale) return;
  const worldX = view.x + sx / view.scale;
  const worldY = view.y + sy / view.scale;
  view.scale = newScale;
  view.x = worldX - sx / newScale;
  view.y = worldY - sy / newScale;
}

/** One numbered marker, screen-sized, with a dark halo for contrast. */
function drawMarker(
  context: CanvasRenderingContext2D,
  marker: SheetMarker,
  view: ViewState,
  core: PaneCore,
  colors: PaneColors,
): void {
  const sx = (marker.x - view.x) * view.scale;
  const sy = (marker.y - view.y) * view.scale;
  const radius = MARKER_RADIUS_PX;
  if (
    sx < -radius * 2 ||
    sy < -radius * 2 ||
    sx > core.width + radius * 2 ||
    sy > core.height + radius * 2
  ) {
    return;
  }

  const color = colors.marker[(marker.number - 1) % colors.marker.length] ?? colors.textLow;
  context.save();
  context.lineWidth = 1;

  if (marker.pending) {
    // The old-side marker waiting for its partner: a dashed ring.
    context.setLineDash([4, 3]);
    context.strokeStyle = colors.textHi;
    context.beginPath();
    context.arc(sx, sy, radius, 0, Math.PI * 2);
    context.stroke();
    context.setLineDash([]);
    context.fillStyle = colors.textHi;
    context.globalAlpha = 0.9;
    context.font = '600 11px "Segoe UI", system-ui, sans-serif';
    context.textAlign = 'center';
    context.textBaseline = 'middle';
    context.fillText(String(marker.number), sx, sy + 0.5);
    context.restore();
    return;
  }

  // A filled marker: dark halo, status-colour core, number on top.
  context.strokeStyle = colors.room;
  context.lineWidth = 4;
  context.beginPath();
  context.arc(sx, sy, radius, 0, Math.PI * 2);
  context.stroke();
  context.fillStyle = color;
  context.beginPath();
  context.arc(sx, sy, radius - 1, 0, Math.PI * 2);
  context.fill();

  context.font = '600 11px "Segoe UI", system-ui, sans-serif';
  context.textAlign = 'center';
  context.textBaseline = 'middle';
  context.lineWidth = 3;
  context.strokeStyle = colors.room;
  context.strokeText(String(marker.number), sx, sy + 0.5);
  context.fillStyle = colors.textHi;
  context.fillText(String(marker.number), sx, sy + 0.5);
  context.restore();
}
