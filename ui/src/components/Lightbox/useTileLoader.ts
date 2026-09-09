/**
 * Tile fetching for the Lightbox viewer.
 *
 * One sheet is rendered as a pyramid of 512 px PNG tiles. The viewer only
 * ever asks for the tiles that are visible (plus one prefetch ring), at the
 * pyramid level whose resolution matches the current zoom — the same
 * strategy a map viewer uses, because an A0 sheet at 200 DPI is ~33,000 px
 * wide and can never exist as a single texture.
 *
 * Responsibilities split cleanly:
 *
 *  - The pure helpers (`pickTileLevel`, `visibleTiles`, `tileSpans`) decide
 *    WHAT to fetch from a manifest and a viewport. They are used by both the
 *    PixiJS path and the Canvas 2D fallback, and they do no I/O.
 *  - The `TileLoader` (created by `createTileLoader`, owned through
 *    `useTileLoader`) does the I/O: manifest fetches, PNG downloads with
 *    `fetch` + `createImageBitmap`, a 6-way concurrency limit, an LRU of
 *    decoded tiles (default 300, the same budget as the GPU texture cache),
 *    and AbortController cancellation of stale requests the moment the
 *    wanted set changes.
 *
 * Geometry model (assumption, mirroring the frozen tile-server contract):
 * a level's `cols` × `rows` tiles partition the sheet uniformly — tile (x,y)
 * covers world pixels x·(width_px/cols) … (x+1)·(width_px/cols). The
 * renderers stretch the 512 px bitmap over that span. Edge tiles therefore
 * may be scaled by a pixel or two at most, invisible at the zoom level that
 * chooses them.
 *
 * There is no UI test infrastructure in this repository yet, so the pure
 * helpers are written to be verified by reading: no module state, no
 * closures over the loader.
 */

import { useEffect, useState } from 'react';

import { fetchTileBlob, fetchTileManifest, tileUrl } from '../../api/client';
import type { TileLevel, TileManifest } from '../../api/types';

/** How many decoded tiles the loader may keep (the plan's 300-tile budget). */
export const DEFAULT_TILE_BUDGET = 300;

/** Parallel PNG downloads the loader will run at most. */
export const DEFAULT_CONCURRENCY = 6;

/** How many tiles beyond the visible edge are prefetched in each direction. */
export const DEFAULT_PREFETCH_RING = 1;

/** A decoded tile image. Both forms draw identically in every renderer. */
export type TileImage = ImageBitmap | HTMLCanvasElement;

/** One tile, identified by its place in a sheet's pyramid. */
export interface TileRef {
  sheetId: string;
  level: number;
  x: number;
  y: number;
}

/** One tile the viewer wants right now, with its cache key and URL. */
export interface VisibleTile extends TileRef {
  key: string;
  url: string;
}

/** The part of the sheet the viewer can see, in world (sheet) pixels. */
export interface TileViewport {
  /** World-pixel x of the left edge of the visible area. */
  x: number;
  /** World-pixel y of the top edge. */
  y: number;
  /** Screen (CSS) pixels per world pixel. */
  scale: number;
  /** Visible width in screen pixels. */
  width: number;
  /** Visible height in screen pixels. */
  height: number;
}

export interface VisibleTilesOptions {
  /** Tiles to prefetch around the visible edge (default 1). */
  ring?: number;
  /** Physical pixels per CSS pixel, for level choice (default 1). */
  devicePixelRatio?: number;
}

/** Cache key for a tile — the loader's LRU and the GPU cache share it. */
export function tileKey(sheetId: string, level: number, x: number, y: number): string {
  return `${sheetId}/${level}/${x}/${y}`;
}

/**
 * How many world pixels one tile spans horizontally and vertically at a
 * level. Null when the manifest or the level entry is unusable.
 */
export function tileSpans(
  manifest: TileManifest,
  levelNumber: number,
): { spanX: number; spanY: number } | null {
  if (!(manifest.width_px > 0) || !(manifest.height_px > 0)) return null;
  const level = manifest.levels.find((entry) => entry.level === levelNumber);
  if (!level || !(level.cols > 0) || !(level.rows > 0)) return null;
  return {
    spanX: manifest.width_px / level.cols,
    spanY: manifest.height_px / level.rows,
  };
}

/**
 * Choose the pyramid level whose texture density is closest to the current
 * screen density: tiles that map ~1 texel onto 1 device pixel. Scanning
 * ascending keeps the finer level on a tie.
 */
export function pickTileLevel(
  manifest: TileManifest,
  devicePxPerWorldPx: number,
): TileLevel | null {
  if (!(manifest.width_px > 0) || !(manifest.tile_size > 0)) return null;

  let best: TileLevel | null = null;
  let bestError = Infinity;
  for (const level of manifest.levels) {
    if (!(level.cols > 0) || !(level.rows > 0)) continue;
    const spanX = manifest.width_px / level.cols;
    const density = manifest.tile_size / spanX; // texels per world pixel
    const error = Math.abs(density - devicePxPerWorldPx);
    if (error < bestError) {
      bestError = error;
      best = level;
    }
  }
  return best;
}

/**
 * Every tile of one sheet that should be on screen or in flight for a
 * viewport: the level closest to 1:1 at `viewport.scale`, clipped to the
 * sheet, plus one ring of neighbours. Pure and synchronous — no fetching.
 */
export function visibleTiles(
  sheetId: string,
  viewport: TileViewport,
  manifest: TileManifest,
  options: VisibleTilesOptions = {},
): VisibleTile[] {
  const ring = Math.max(0, Math.floor(options.ring ?? DEFAULT_PREFETCH_RING));
  const devicePixelRatio = Math.max(options.devicePixelRatio ?? 1, 0.25);

  if (!(viewport.scale > 0) || !(viewport.width > 0) || !(viewport.height > 0)) return [];

  const level = pickTileLevel(manifest, viewport.scale * devicePixelRatio);
  if (!level || !(level.cols > 0) || !(level.rows > 0)) return [];
  if (!(manifest.width_px > 0) || !(manifest.height_px > 0)) return [];

  const spanX = manifest.width_px / level.cols;
  const spanY = manifest.height_px / level.rows;

  const worldLeft = viewport.x;
  const worldRight = viewport.x + viewport.width / viewport.scale;
  const worldTop = viewport.y;
  const worldBottom = viewport.y + viewport.height / viewport.scale;

  const x0 = Math.max(0, Math.floor(worldLeft / spanX) - ring);
  const x1 = Math.min(level.cols - 1, Math.floor(worldRight / spanX) + ring);
  const y0 = Math.max(0, Math.floor(worldTop / spanY) - ring);
  const y1 = Math.min(level.rows - 1, Math.floor(worldBottom / spanY) + ring);

  if (x1 < x0 || y1 < y0) return [];

  const tiles: VisibleTile[] = [];
  for (let y = y0; y <= y1; y++) {
    for (let x = x0; x <= x1; x++) {
      tiles.push({
        sheetId,
        level: level.level,
        x,
        y,
        key: tileKey(sheetId, level.level, x, y),
        url: tileUrl(sheetId, level.level, x, y),
      });
    }
  }
  return tiles;
}

export interface TileLoaderOptions {
  budget?: number;
  concurrency?: number;
}

/**
 * The fetching half of the viewer. `destroy()` is idempotent and resets the
 * loader completely (aborting everything in flight), which is what makes
 * React 19 StrictMode's double-mount safe: the instance survives the
 * development mount/unmount cycle and simply restarts fresh.
 */
export interface TileLoader {
  readonly budget: number;
  readonly concurrency: number;

  /** Fetch (once) and remember a sheet's manifest. Null when it failed. */
  ensureManifest(sheetId: string): Promise<TileManifest | null>;
  /** The manifest fetched so far, if one exists. Synchronous for drawing. */
  getManifest(sheetId: string): TileManifest | null;

  /** Sheets the viewer is showing; requests outside them are cancelled. */
  setActiveSheets(sheetIds: string[]): void;
  /**
   * The tiles wanted right now. Requests no longer wanted are aborted;
   * wanted tiles that are not cached, in flight or known-broken are queued.
   */
  want(tiles: ReadonlyArray<TileRef>): void;

  /** Cached decoded image, or undefined. Refreshes the LRU position. */
  getBitmap(key: string): TileImage | undefined;
  /** True while the tile is queued or downloading. */
  isPending(key: string): boolean;
  /** True when the last attempt failed (the tile stays a quiet placeholder). */
  isErrored(key: string): boolean;

  /** Called once when a tile finishes decoding and enters the cache. */
  onTile(callback: (key: string) => void): () => void;

  /** Forget one sheet entirely: network, cache, manifest. */
  releaseSheet(sheetId: string): void;
  /** Cancel everything and drop every cache. Idempotent; the loader stays usable. */
  destroy(): void;
}

function decodeTileImage(blob: Blob): Promise<TileImage> {
  if (typeof createImageBitmap === 'function') return createImageBitmap(blob);

  // Very old WebView2 builds lack createImageBitmap: decode through an <img>
  // and copy into a canvas, which every renderer path can draw.
  const objectUrl = URL.createObjectURL(blob);
  return new Promise<TileImage>((resolve, reject) => {
    const image = new Image();
    image.onload = () => {
      const canvas = document.createElement('canvas');
      canvas.width = image.naturalWidth;
      canvas.height = image.naturalHeight;
      const context = canvas.getContext('2d');
      if (!context) {
        reject(new Error('Canvas 2D is unavailable.'));
        return;
      }
      context.drawImage(image, 0, 0);
      resolve(canvas);
    };
    image.onerror = () => reject(new Error('The tile image could not be decoded.'));
    image.src = objectUrl;
  }).finally(() => {
    URL.revokeObjectURL(objectUrl);
  });
}

class TileLoaderImpl implements TileLoader {
  readonly budget: number;
  readonly concurrency: number;

  /** Decoded images, LRU: re-inserted on use, oldest evicted over budget. */
  private readonly cache = new Map<string, TileImage>();
  /** Where each key came from, so a queued key can be fetched later. */
  private readonly refs = new Map<string, TileRef>();
  private readonly queue: string[] = [];
  private readonly inFlight = new Map<string, AbortController>();
  private readonly errored = new Set<string>();
  private activeCount = 0;

  private readonly manifests = new Map<string, TileManifest | null>();
  private readonly manifestMemo = new Map<string, Promise<TileManifest | null>>();

  private readonly listeners = new Set<(key: string) => void>();

  constructor(options: TileLoaderOptions = {}) {
    this.budget = Math.max(1, options.budget ?? DEFAULT_TILE_BUDGET);
    this.concurrency = Math.max(1, options.concurrency ?? DEFAULT_CONCURRENCY);
  }

  ensureManifest(sheetId: string): Promise<TileManifest | null> {
    const memoized = this.manifestMemo.get(sheetId);
    if (memoized) return memoized;

    const request = fetchTileManifest(sheetId)
      .then((manifest) => {
        this.manifests.set(sheetId, manifest);
        return manifest;
      })
      .catch(() => {
        // Remember the failure but drop the memo so the next content sync
        // (or a later sheet change) tries again instead of giving up forever.
        this.manifests.set(sheetId, null);
        this.manifestMemo.delete(sheetId);
        return null;
      });
    this.manifestMemo.set(sheetId, request);
    return request;
  }

  getManifest(sheetId: string): TileManifest | null {
    return this.manifests.get(sheetId) ?? null;
  }

  setActiveSheets(sheetIds: string[]): void {
    const active = new Set(sheetIds);
    for (const [key, controller] of this.inFlight) {
      const ref = this.refs.get(key);
      if (ref && !active.has(ref.sheetId)) controller.abort();
    }
    this.dropQueued((key) => {
      const ref = this.refs.get(key);
      return ref !== undefined && !active.has(ref.sheetId);
    });
  }

  want(tiles: ReadonlyArray<TileRef>): void {
    const wanted = new Set<string>();
    for (const tile of tiles) {
      const key = tileKey(tile.sheetId, tile.level, tile.x, tile.y);
      wanted.add(key);
      if (!this.refs.has(key)) this.refs.set(key, tile);
    }

    // Requests the viewer no longer wants are stale: cancel them now, not
    // when they land, so bandwidth and decode time go to the new view.
    for (const [key, controller] of this.inFlight) {
      if (!wanted.has(key)) controller.abort();
    }
    this.dropQueued((key) => !wanted.has(key));

    for (const key of wanted) {
      if (this.cache.has(key) || this.inFlight.has(key) || this.errored.has(key)) continue;
      if (!this.queue.includes(key)) this.queue.push(key);
    }
    this.pump();
  }

  getBitmap(key: string): TileImage | undefined {
    const image = this.cache.get(key);
    if (image === undefined) return undefined;
    // Touch: the most recently used tile survives eviction longest.
    this.cache.delete(key);
    this.cache.set(key, image);
    return image;
  }

  isPending(key: string): boolean {
    return this.inFlight.has(key) || this.queue.includes(key);
  }

  isErrored(key: string): boolean {
    return this.errored.has(key);
  }

  onTile(callback: (key: string) => void): () => void {
    this.listeners.add(callback);
    return () => this.listeners.delete(callback);
  }

  releaseSheet(sheetId: string): void {
    const prefix = `${sheetId}/`;
    for (const [key, controller] of this.inFlight) {
      if (key.startsWith(prefix)) controller.abort();
    }
    this.dropQueued((key) => key.startsWith(prefix));
    for (const key of [...this.refs.keys()]) {
      if (key.startsWith(prefix)) this.refs.delete(key);
    }
    for (const key of [...this.cache.keys()]) {
      if (key.startsWith(prefix)) this.cache.delete(key);
    }
    for (const key of [...this.errored]) {
      if (key.startsWith(prefix)) this.errored.delete(key);
    }
    this.manifests.delete(sheetId);
    this.manifestMemo.delete(sheetId);
  }

  destroy(): void {
    for (const controller of this.inFlight.values()) controller.abort();
    this.inFlight.clear();
    this.activeCount = 0;
    this.queue.length = 0;
    this.refs.clear();
    this.cache.clear();
    this.errored.clear();
    this.manifests.clear();
    this.manifestMemo.clear();
  }

  private dropQueued(predicate: (key: string) => boolean): void {
    if (this.queue.length === 0) return;
    let changed = false;
    const kept: string[] = [];
    for (const key of this.queue) {
      if (predicate(key)) changed = true;
      else kept.push(key);
    }
    if (changed) {
      this.queue.length = 0;
      this.queue.push(...kept);
    }
  }

  private pump(): void {
    while (this.activeCount < this.concurrency) {
      const key = this.queue.shift();
      if (key === undefined) break;
      if (this.cache.has(key) || this.errored.has(key)) continue;
      const ref = this.refs.get(key);
      if (!ref) continue;
      this.activeCount++;
      void this.runRequest(key, ref);
    }
  }

  private async runRequest(key: string, ref: TileRef): Promise<void> {
    const controller = new AbortController();
    this.inFlight.set(key, controller);
    try {
      const blob = await fetchTileBlob(ref.sheetId, ref.level, ref.x, ref.y, controller.signal);
      const image = await decodeTileImage(blob);
      if (controller.signal.aborted || this.inFlight.get(key) !== controller) return;
      this.cache.set(key, image);
      this.trim();
      this.errored.delete(key);
      for (const callback of this.listeners) callback(key);
    } catch {
      // Cancelled work stays silent; a genuine failure makes the tile a
      // quiet placeholder rather than a stream of error messages.
      if (!controller.signal.aborted) this.errored.add(key);
    } finally {
      this.inFlight.delete(key);
      this.activeCount = Math.max(0, this.activeCount - 1);
      this.pump();
    }
  }

  private trim(): void {
    while (this.cache.size > this.budget) {
      const oldest = this.cache.keys().next().value;
      if (oldest === undefined) break;
      this.cache.delete(oldest);
    }
  }
}

/** A fresh loader with the default budget and concurrency. */
export function createTileLoader(options: TileLoaderOptions = {}): TileLoader {
  return new TileLoaderImpl(options);
}

/**
 * The loader for one Lightbox mount. The lazy `useState` initializer keeps
 * one instance per mount without touching a ref during render; option
 * changes after mount are ignored by design. `destroy()` is idempotent and
 * resets the loader, so the StrictMode double-mount cycle cannot leak
 * requests — the instance simply restarts fresh on the second mount pass.
 */
export function useTileLoader(options: TileLoaderOptions = {}): TileLoader {
  const [loader] = useState<TileLoader>(() => createTileLoader(options));
  useEffect(() => () => loader.destroy(), [loader]);
  return loader;
}
