/**
 * Lightbox — the tiled drawing viewer core (Phase 4, Task 4.11).
 *
 * Two rendered sheets (old and new) are drawn from PNG tiles served by the
 * engine at `{base}/api/tiles/{sheet_id}/...`. Only the tiles visible in the
 * viewport plus one prefetch ring are ever loaded; decoded tiles and GPU
 * textures each live under a 300-tile LRU budget. The old sheet carries the
 * engine's alignment matrix as a PixiJS (or Canvas 2D) affine transform, so
 * aligning is pure client-side geometry — no resampling ever happened in
 * Python and none happens here either.
 *
 * Renderers:
 *  - WebGL through PixiJS when available (the normal path).
 *  - A plain Canvas 2D path when WebGL is missing or Pixi fails to start.
 *    It draws the same visible tiles into a 2D context with the same
 *    interactions, and a non-blocking `role="status"` note says so.
 *
 * Interaction (all of it moves the container transform only — textures are
 * never re-created while panning):
 *  - drag to pan, wheel zooms around the pointer, pinch on touch
 *  - keyboard: arrows pan, +/− zoom, 0 fits, 1 = 100 %
 *
 * Chrome drawn over the room background, never over the drawing:
 *  - a minimap (plain 2D canvas in the corner) redrawn at ~15 fps
 *  - a scale readout: zoom percent, and — from `pxPerMm` (falling back to
 *    `dpi / 25.4`) — what 100 mm on paper measures at the current zoom.
 *
 * Props are kept deliberately minimal: view modes (single sheet, swipe,
 * blink, overlay opacity) are Task 4.12 and will add their own controls.
 * `initialMode` is accepted now only so the prop surface stays stable; this
 * core always stacks both layers, old beneath new.
 *
 * Lifecycle is one effect: it creates everything and tears everything down
 * (Pixi app, textures, observers, listeners). Cleanup is idempotent, which
 * makes React 19 StrictMode's double-mount safe.
 */

import { useEffect, useRef, useState } from 'react';
import {
  Application,
  Container,
  Graphics,
  ImageSource,
  Matrix,
  Sprite,
  Texture,
  isWebGLSupported,
} from 'pixi.js';
import type { TileManifest } from '../../api/types';
import { pixiMatrixFromAlignment } from './pixiMatrix';
import { tileSpans, useTileLoader, visibleTiles } from './useTileLoader';
import type { TileLoader, TileViewport, VisibleTile } from './useTileLoader';

import './Lightbox.css';

/** GPU texture budget, matching the loader's decoded-tile budget. */
const TILE_TEXTURE_BUDGET = 300;
/** One tile of margin prefetched around the visible edge. */
const PREFETCH_RING = 1;
/** Zoom range, in screen pixels per sheet pixel. */
const MIN_SCALE = 0.005;
const MAX_SCALE = 64;
/** Wheel zoom rate: one click (deltaY ≈ ±100) zooms about 15 %. */
const WHEEL_ZOOM_RATE = 0.0016;
/** One arrow-key press pans this many screen pixels. */
const KEY_PAN_PX = 48;
/** Keyboard zoom factor per press. */
const KEY_ZOOM_FACTOR = 1.25;
/** Minimap redraw throttle in frames (60 fps → ~15 fps). */
const MINIMAP_EVERY_FRAMES = 4;
/** Placeholder (tile still loading) alpha over the sheet. */
const PLACEHOLDER_ALPHA = 0.55;

export type LightboxRendererMode = 'webgl' | 'canvas2d';

export interface LightboxProps {
  /** Engine sheet id for the previous issue; null hides that layer. */
  oldSheetId: string | null;
  /** Engine sheet id for the current issue; null hides that layer. */
  newSheetId: string | null;
  /**
   * Engine alignment matrix, row-major 3×3 mapping old pixels → new pixels.
   * Null (or absent) draws the old sheet in place, unaligned.
   */
  transformMatrix?: number[] | null;
  /** Resolution the sheets were rendered at. Defaults to 200. */
  dpi?: number;
  /** Pixels per millimetre of the rendered sheet; defaults to dpi / 25.4. */
  pxPerMm?: number;
  /**
   * Accepted for forward compatibility with the Task 4.12 view modes.
   * This core ignores it: both layers are always stacked (old below new).
   */
  initialMode?: 'overlay' | 'single-old' | 'single-new';
  /** Extra class for the root element, e.g. a screen's layout class. */
  className?: string;
}

interface ViewState {
  /** World-pixel x at the left edge of the viewport. */
  x: number;
  /** World-pixel y at the top edge of the viewport. */
  y: number;
  /** Screen (CSS) pixels per world pixel. */
  scale: number;
}

/** One visible tile with its on-screen rect already computed. */
interface SceneTile extends VisibleTile {
  rect: { x: number; y: number; w: number; h: number };
}

/** Everything the renderers need to draw one sheet. */
interface LayerScene {
  sheetId: string;
  manifest: TileManifest;
  /** Local (sheet px) → world px. Null means identity (the new sheet). */
  matrix: Matrix | null;
  widthPx: number;
  heightPx: number;
  tiles: SceneTile[];
}

interface Scene {
  old: LayerScene | null;
  new: LayerScene | null;
}

/** Room/light tokens read once into values the renderers can use. */
interface TokenColors {
  room: string;
  sheet: string;
  sheetDim: string;
  textMid: string;
  textHi: string;
}

/** What the render loops need from their host (both backends share it). */
interface FrameHost {
  loader: TileLoader;
  colors: TokenColors;
  /** Called once per drawn frame; the host throttles minimap redraws. */
  onFrame(): void;
}

interface Backend {
  readonly kind: 'webgl' | 'canvas2d';
  /** Create the canvas and start drawing. Throws when unavailable. */
  init(stage: HTMLDivElement): Promise<void>;
  destroy(): void;
  resize(width: number, height: number, devicePixelRatio: number): void;
  /** Viewport changed: cheap transform update, no texture work. */
  applyView(view: ViewState): void;
  /** The visible tile set changed (pan crossed a tile, a tile arrived…). */
  setScene(scene: Scene): void;
}

const DEFAULT_COLORS: TokenColors = {
  room: '#14181c',
  sheet: '#f7f6f3',
  sheetDim: '#e8e6e1',
  textMid: '#9da9b5',
  textHi: '#edf1f5',
};

/** Tokens are the single source of truth; these fallbacks mirror them. */
function readTokenColors(): TokenColors {
  const read = (name: string, fallback: string): string => {
    const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value.length > 0 ? value : fallback;
  };
  return {
    room: read('--room-900', DEFAULT_COLORS.room),
    sheet: read('--sheet', DEFAULT_COLORS.sheet),
    sheetDim: read('--sheet-dim', DEFAULT_COLORS.sheetDim),
    textMid: read('--text-mid', DEFAULT_COLORS.textMid),
    textHi: read('--text-hi', DEFAULT_COLORS.textHi),
  };
}

function clampScale(scale: number): number {
  if (!Number.isFinite(scale)) return 1;
  return Math.min(MAX_SCALE, Math.max(MIN_SCALE, scale));
}

function matrixSignature(matrix: Matrix | null): string {
  if (!matrix) return 'identity';
  return `${matrix.a},${matrix.b},${matrix.c},${matrix.d},${matrix.tx},${matrix.ty}`;
}

// ═══════════════════════════════════════════════════════════════════════
// WebGL backend (PixiJS)
// ═══════════════════════════════════════════════════════════════════════

interface PixiLayerObjects {
  container: Container;
  backdrop: Graphics;
  placeholders: Graphics;
  tiles: Container;
  sprites: Map<string, Sprite>;
  matrixKey: string;
  backdropKey: string;
  placeholderKey: string;
}

function newPixiLayer(): PixiLayerObjects {
  const container = new Container();
  container.visible = false;
  const backdrop = new Graphics();
  const placeholders = new Graphics();
  const tiles = new Container();
  container.addChild(backdrop, placeholders, tiles);
  return {
    container,
    backdrop,
    placeholders,
    tiles,
    sprites: new Map(),
    matrixKey: '',
    backdropKey: '',
    placeholderKey: '',
  };
}

class PixiBackend implements Backend {
  readonly kind = 'webgl' as const;

  private readonly host: FrameHost;
  private app: Application | null = null;
  private world: Container | null = null;
  /** Old under new: added to the world in that order. */
  private readonly layerOrder: Array<'old' | 'new'> = ['old', 'new'];
  private layerObjects = new Map<'old' | 'new', PixiLayerObjects>();
  /** GPU textures across both layers, LRU by re-insertion on use. */
  private textures = new Map<string, Texture>();
  private destroyed = false;

  constructor(host: FrameHost) {
    this.host = host;
  }

  async init(stage: HTMLDivElement): Promise<void> {
    if (typeof isWebGLSupported === 'function' && !isWebGLSupported()) {
      throw new Error('WebGL is not available.');
    }

    const app = new Application();
    try {
      await app.init({
        preference: ['webgl'],
        width: 1,
        height: 1,
        resolution: 1,
        antialias: true,
        background: this.host.colors.room,
      });
    } catch (error) {
      // A context could not be created (driver blocked, headless shell…).
      app.destroy(true);
      throw error;
    }
    if (this.destroyed) {
      app.destroy(true);
      throw new Error('The WebGL backend was destroyed while starting.');
    }

    this.app = app;
    this.world = new Container();
    app.stage.addChild(this.world);
    for (const id of this.layerOrder) {
      const layer = newPixiLayer();
      this.layerObjects.set(id, layer);
      this.world.addChild(layer.container);
    }
    app.canvas.className = 'lightbox__pixi-canvas';
    stage.appendChild(app.canvas);
    app.ticker.add(() => this.host.onFrame());
  }

  destroy(): void {
    if (this.destroyed) return;
    this.destroyed = true;
    for (const texture of this.textures.values()) texture.destroy(true);
    this.textures.clear();
    for (const layer of this.layerObjects.values()) {
      layer.sprites.clear();
    }
    this.layerObjects.clear();
    const app = this.app;
    this.app = null;
    this.world = null;
    if (app) app.destroy(true, { children: true, context: true });
  }

  resize(width: number, height: number, devicePixelRatio: number): void {
    const app = this.app;
    if (!app || !(width > 0) || !(height > 0)) return;
    app.renderer.resize(width, height, devicePixelRatio);
    app.canvas.style.width = `${width}px`;
    app.canvas.style.height = `${height}px`;
  }

  applyView(view: ViewState): void {
    const world = this.world;
    if (!world || !(view.scale > 0)) return;
    world.position.set(-view.x * view.scale, -view.y * view.scale);
    world.scale.set(view.scale, view.scale);
  }

  setScene(scene: Scene): void {
    if (!this.app) return;

    // The eviction budget spans both layers: a tile stays cached while it
    // is wanted by either of them.
    const wanted = new Map<string, SceneTile>();
    for (const id of this.layerOrder) {
      const layer = scene[id];
      if (!layer) continue;
      for (const tile of layer.tiles) wanted.set(tile.key, tile);
    }

    for (const id of this.layerOrder) {
      const layer = scene[id];
      const objects = this.layerObjects.get(id);
      if (!objects) continue;
      if (layer === null) {
        objects.container.visible = false;
        continue;
      }
      this.syncLayer(layer, objects);
    }
    this.evictTextures(wanted);
  }

  private syncLayer(layer: LayerScene, objects: PixiLayerObjects): void {
    objects.container.visible = true;

    // The old layer carries the alignment matrix; the new one is identity.
    const matrix = layer.matrix;
    const matrixKey = matrixSignature(matrix);
    if (matrixKey !== objects.matrixKey) {
      objects.container.setFromMatrix(matrix ?? new Matrix());
      objects.matrixKey = matrixKey;
    }

    // The sheet backdrop (rebuilt only when the sheet size changes).
    const backdropKey = `${layer.widthPx}x${layer.heightPx}`;
    if (backdropKey !== objects.backdropKey) {
      objects.backdrop.clear();
      objects.backdrop.rect(0, 0, layer.widthPx, layer.heightPx);
      objects.backdrop.fill(this.host.colors.sheet);
      objects.backdropKey = backdropKey;
    }

    // Only this layer's own tiles become sprites here; a tile key can only
    // belong to one layer (keys start with the sheet id).
    const desired = new Map<string, SceneTile>();
    for (const tile of layer.tiles) desired.set(tile.key, tile);

    // Drop sprites the view no longer wants. Their textures stay cached.
    for (const [tileKey, sprite] of objects.sprites) {
      if (!desired.has(tileKey)) {
        sprite.destroy();
        objects.sprites.delete(tileKey);
      }
    }

    // Touch every wanted texture (LRU order) and create missing sprites.
    for (const [tileKey, tile] of desired) {
      const texture = this.textures.get(tileKey);
      if (texture) {
        this.textures.delete(tileKey);
        this.textures.set(tileKey, texture);
      }
      if (objects.sprites.has(tileKey)) continue;
      const image = this.host.loader.getBitmap(tileKey);
      if (image === undefined) continue; // still loading → placeholder below
      const nextTexture = texture ?? this.createTexture(tileKey, image);
      const sprite = new Sprite(nextTexture);
      sprite.x = tile.rect.x;
      sprite.y = tile.rect.y;
      sprite.width = tile.rect.w;
      sprite.height = tile.rect.h;
      objects.sprites.set(tileKey, sprite);
      objects.tiles.addChild(sprite);
    }

    // Subtle placeholders for the tiles that are still on their way. They
    // sit under the sprites, so the moment a tile lands it covers its own.
    const pending: SceneTile[] = [];
    for (const [tileKey, tile] of desired) {
      if (!objects.sprites.has(tileKey)) pending.push(tile);
    }
    const placeholderKey = pending
      .map((tile) => tile.key)
      .sort()
      .join('|');
    if (placeholderKey !== objects.placeholderKey) {
      objects.placeholderKey = placeholderKey;
      objects.placeholders.clear();
      if (pending.length > 0) {
        for (const tile of pending) {
          objects.placeholders.rect(tile.rect.x, tile.rect.y, tile.rect.w, tile.rect.h);
        }
        objects.placeholders.fill(this.host.colors.sheetDim, PLACEHOLDER_ALPHA);
      }
    }
  }

  private createTexture(tileKey: string, image: ImageBitmap | HTMLCanvasElement): Texture {
    const source = new ImageSource({
      resource: image,
      label: tileKey,
      autoGarbageCollect: false,
    });
    const texture = new Texture({ source });
    this.textures.set(tileKey, texture);
    return texture;
  }

  /** Keep the GPU cache inside its budget; non-visible tiles go first. */
  private evictTextures(wanted: ReadonlyMap<string, SceneTile>): void {
    while (this.textures.size > TILE_TEXTURE_BUDGET) {
      let victim: string | undefined;
      for (const tileKey of this.textures.keys()) {
        if (!wanted.has(tileKey)) {
          victim = tileKey;
          break;
        }
      }
      if (victim === undefined) victim = this.textures.keys().next().value;
      if (victim === undefined) break;
      this.dropTexture(victim);
    }
  }

  private dropTexture(tileKey: string): void {
    const texture = this.textures.get(tileKey);
    if (texture) {
      this.textures.delete(tileKey);
      texture.destroy(true);
    }
    for (const objects of this.layerObjects.values()) {
      const sprite = objects.sprites.get(tileKey);
      if (sprite) {
        sprite.destroy();
        objects.sprites.delete(tileKey);
      }
    }
  }
}

// ═══════════════════════════════════════════════════════════════════════
// Canvas 2D fallback backend
// ═══════════════════════════════════════════════════════════════════════

class Canvas2dBackend implements Backend {
  readonly kind = 'canvas2d' as const;

  private readonly host: FrameHost;
  private canvas: HTMLCanvasElement | null = null;
  private context: CanvasRenderingContext2D | null = null;
  private rafId = 0;
  private destroyed = false;
  private width = 0;
  private height = 0;
  private devicePixelRatio = 1;
  private view: ViewState = { x: 0, y: 0, scale: 1 };
  private scene: Scene = { old: null, new: null };

  constructor(host: FrameHost) {
    this.host = host;
  }

  async init(stage: HTMLDivElement): Promise<void> {
    const canvas = document.createElement('canvas');
    canvas.className = 'lightbox__canvas2d-canvas';
    const context = canvas.getContext('2d');
    if (!context) throw new Error('Canvas 2D is not available.');
    stage.appendChild(canvas);
    this.canvas = canvas;
    this.context = context;
    context.imageSmoothingEnabled = true;
    this.loop();
  }

  destroy(): void {
    if (this.destroyed) return;
    this.destroyed = true;
    if (this.rafId !== 0) cancelAnimationFrame(this.rafId);
    this.rafId = 0;
    this.canvas?.remove();
    this.canvas = null;
    this.context = null;
  }

  resize(width: number, height: number, devicePixelRatio: number): void {
    if (!this.canvas || !(width > 0) || !(height > 0)) return;
    this.width = width;
    this.height = height;
    this.devicePixelRatio = devicePixelRatio;
    this.canvas.width = Math.max(1, Math.round(width * devicePixelRatio));
    this.canvas.height = Math.max(1, Math.round(height * devicePixelRatio));
    this.canvas.style.width = `${width}px`;
    this.canvas.style.height = `${height}px`;
  }

  applyView(view: ViewState): void {
    this.view = { ...view };
  }

  setScene(scene: Scene): void {
    this.scene = scene;
  }

  private loop = (): void => {
    if (this.destroyed) return;
    this.rafId = requestAnimationFrame(this.loop);
    this.draw();
    this.host.onFrame();
  };

  private draw(): void {
    const context = this.context;
    if (!context || !(this.width > 0) || !(this.height > 0)) return;
    const { colors } = this.host;

    context.setTransform(this.devicePixelRatio, 0, 0, this.devicePixelRatio, 0, 0);
    context.fillStyle = colors.room;
    context.fillRect(0, 0, this.width, this.height);

    const view = this.view;
    if (!(view.scale > 0)) return;
    const scene = this.scene;
    if (!scene.old && !scene.new) return;

    context.save();
    context.transform(view.scale, 0, 0, view.scale, -view.x * view.scale, -view.y * view.scale);
    this.drawLayer(context, scene.old);
    this.drawLayer(context, scene.new);
    context.restore();
  }

  private drawLayer(context: CanvasRenderingContext2D, layer: LayerScene | null): void {
    if (!layer) return;
    const { colors, loader } = this.host;

    context.save();
    const matrix = layer.matrix;
    if (matrix) context.transform(matrix.a, matrix.b, matrix.c, matrix.d, matrix.tx, matrix.ty);

    context.fillStyle = colors.sheet;
    context.fillRect(0, 0, layer.widthPx, layer.heightPx);

    for (const tile of layer.tiles) {
      const image = loader.getBitmap(tile.key);
      if (image !== undefined) {
        context.drawImage(image, tile.rect.x, tile.rect.y, tile.rect.w, tile.rect.h);
      } else {
        // Subtle placeholder: the sheet is there, this tile is still coming.
        context.globalAlpha = PLACEHOLDER_ALPHA;
        context.fillStyle = colors.sheetDim;
        context.fillRect(tile.rect.x, tile.rect.y, tile.rect.w, tile.rect.h);
        context.globalAlpha = 1;
      }
    }
    context.restore();
  }
}

// ═══════════════════════════════════════════════════════════════════════
// The core: content, viewport and chrome above the renderers
// ═══════════════════════════════════════════════════════════════════════

interface LightboxCoreOptions {
  loader: TileLoader;
  stage: HTMLDivElement;
  minimapCanvas: HTMLCanvasElement;
  scaleElement: HTMLDivElement;
  colors: TokenColors;
  onMode: (mode: LightboxRendererMode) => void;
}

class LightboxCore implements FrameHost {
  readonly loader: TileLoader;
  readonly colors: TokenColors;

  private readonly stage: HTMLDivElement;
  private readonly minimapCanvas: HTMLCanvasElement;
  private readonly scaleElement: HTMLDivElement;
  private readonly onMode: (mode: LightboxRendererMode) => void;

  private backend: Backend | null = null;
  private disposed = false;
  private readonly tileUnsubscribe: () => void;

  private oldSheetId: string | null = null;
  private newSheetId: string | null = null;
  private transformMatrix: number[] | null = null;
  private oldMatrix: Matrix | null = null;
  private dpi = 200;
  private pxPerMm: number | null = null;

  private readonly view: ViewState = { x: 0, y: 0, scale: 1 };
  private width = 0;
  private height = 0;
  private devicePixelRatio = 1;

  private resizeObserver: ResizeObserver | null = null;
  private lastFitKey = '';
  private frame = 0;
  private minimapDirty = false;
  private lastMinimapAt = 0;
  private minimapBufferKey = '';

  constructor(options: LightboxCoreOptions) {
    this.loader = options.loader;
    this.colors = options.colors;
    this.stage = options.stage;
    this.minimapCanvas = options.minimapCanvas;
    this.scaleElement = options.scaleElement;
    this.onMode = options.onMode;
    // Tiles arrive asynchronously, long after the reconcile that requested
    // them; each arrival must push the new bitmaps into the scene or the
    // placeholder would sit there until the next user interaction.
    this.tileUnsubscribe = this.loader.onTile(() => this.onTileArrived());
  }

  private onTileArrived(): void {
    if (this.disposed) return;
    if (!this.backend) return;
    this.backend.setScene(this.buildScene());
  }

  /** Build the best renderer available, then reconcile everything. */
  async start(): Promise<void> {
    let backend: Backend | null = new PixiBackend(this);
    try {
      await backend.init(this.stage);
    } catch {
      backend.destroy();
      backend = null;
    }
    if (!backend) {
      backend = new Canvas2dBackend(this);
      try {
        await backend.init(this.stage);
      } catch {
        backend.destroy();
        backend = null;
      }
    }
    if (this.disposed) {
      backend?.destroy();
      return;
    }
    if (!backend) return;
    this.backend = backend;
    this.onMode(backend.kind);

    this.syncSize();
    this.observeSize();
    this.contentChanged();
  }

  destroy(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.tileUnsubscribe();
    this.resizeObserver?.disconnect();
    this.resizeObserver = null;
    window.removeEventListener('resize', this.onDomResize);
    this.backend?.destroy();
    this.backend = null;
  }

  // ── Props from React ─────────────────────────────────────────────────

  setContent(next: {
    oldSheetId: string | null;
    newSheetId: string | null;
    transformMatrix: number[] | null;
  }): void {
    const oldMatrix = pixiMatrixFromAlignment(next.transformMatrix);
    const previous = { oldSheetId: this.oldSheetId, newSheetId: this.newSheetId };
    const same =
      next.oldSheetId === previous.oldSheetId &&
      next.newSheetId === previous.newSheetId &&
      matrixSignature(oldMatrix) === matrixSignature(this.oldMatrix);
    if (same) return;

    this.oldSheetId = next.oldSheetId;
    this.newSheetId = next.newSheetId;
    this.transformMatrix = next.transformMatrix;
    this.oldMatrix = oldMatrix;

    // Sheets that left the comparison can drop their cached state now.
    const previousIds = [previous.oldSheetId, previous.newSheetId].filter(
      (id): id is string => id !== null,
    );
    const keep = new Set(
      [next.oldSheetId, next.newSheetId].filter((id): id is string => id !== null),
    );
    for (const id of previousIds) {
      if (!keep.has(id)) this.loader.releaseSheet(id);
    }

    // Force a fresh fit once the new content's sizes are known.
    this.lastFitKey = '';
    this.contentChanged();
  }

  setViewMeta(next: { dpi: number; pxPerMm: number | null }): void {
    this.dpi = next.dpi;
    this.pxPerMm = next.pxPerMm;
    this.refreshScaleIndicator();
  }

  private contentChanged(): void {
    const ids = [this.oldSheetId, this.newSheetId].filter((id): id is string => id !== null);
    this.loader.setActiveSheets(ids);
    for (const id of ids) {
      void this.loader.ensureManifest(id).then(() => {
        if (this.disposed) return;
        // A manifest arrived (or failed): sizes may be known now, so the
        // pending fit can run and tiles can be requested for that sheet.
        this.onContentReady();
      });
    }
    this.onContentReady();
  }

  private onContentReady(): void {
    this.maybeFit();
    this.reconcile();
  }

  // ── Sizing ───────────────────────────────────────────────────────────

  private observeSize(): void {
    if (typeof ResizeObserver === 'undefined') {
      // Ancient shells: fall back to window resize events.
      window.addEventListener('resize', this.onDomResize);
      return;
    }
    const observer = new ResizeObserver(() => this.syncSize());
    observer.observe(this.stage);
    this.resizeObserver = observer;
  }

  private readonly onDomResize = (): void => this.syncSize();

  private syncSize(): void {
    const rect = this.stage.getBoundingClientRect();
    this.setSize(Math.round(rect.width), Math.round(rect.height));
  }

  private setSize(width: number, height: number): void {
    if (!(width > 0) || !(height > 0)) return;
    const changed = width !== this.width || height !== this.height;
    this.width = width;
    this.height = height;
    this.devicePixelRatio = window.devicePixelRatio || 1;
    // Push unconditionally: the backend may have appeared between resizes
    // (async Pixi init) and needs the current size even when it did not
    // change since the last observation.
    this.backend?.resize(width, height, this.devicePixelRatio);
    if (!changed) return;
    this.maybeFit();
    this.applyView();
  }

  // ── Viewport commands (from pointer, wheel and keyboard) ─────────────

  /** Pan by a screen delta: content follows the pointer. */
  panByScreen(dxScreen: number, dyScreen: number): void {
    if (!(this.view.scale > 0)) return;
    this.view.x -= dxScreen / this.view.scale;
    this.view.y -= dyScreen / this.view.scale;
    this.applyView();
  }

  /** Zoom by `factor`, keeping the world point under (sx, sy) fixed. */
  zoomAt(sx: number, sy: number, factor: number): void {
    if (!(this.width > 0) || !(this.height > 0)) return;
    const oldScale = this.view.scale;
    const newScale = clampScale(oldScale * factor);
    if (newScale === oldScale) return;
    const worldX = this.view.x + sx / oldScale;
    const worldY = this.view.y + sy / oldScale;
    this.view.scale = newScale;
    this.view.x = worldX - sx / newScale;
    this.view.y = worldY - sy / newScale;
    this.applyView();
  }

  zoomAtCenter(factor: number): void {
    this.zoomAt(this.width / 2, this.height / 2, factor);
  }

  /** Fit the union of both sheets (transformed old sheet included). */
  fit(): void {
    const bounds = this.contentBounds();
    if (!bounds) return;
    const padX = Math.max(24, this.width * 0.03);
    const padY = Math.max(24, this.height * 0.03);
    const availableWidth = Math.max(1, this.width - padX * 2);
    const availableHeight = Math.max(1, this.height - padY * 2);
    const scale = clampScale(
      Math.min(availableWidth / bounds.w, availableHeight / bounds.h),
    );
    this.view.scale = scale;
    this.view.x = bounds.x + bounds.w / 2 - this.width / (2 * scale);
    this.view.y = bounds.y + bounds.h / 2 - this.height / (2 * scale);
    this.applyView();
  }

  /** 100 %: one sheet pixel per screen pixel, viewport centred. */
  zoom100(): void {
    this.zoomAt(this.width / 2, this.height / 2, 1 / this.view.scale);
  }

  // ── FrameHost ────────────────────────────────────────────────────────

  onFrame(): void {
    this.frame++;
    const now = performance.now();
    if (!this.minimapDirty && this.frame % MINIMAP_EVERY_FRAMES !== 0) return;
    if (now - this.lastMinimapAt < 60) return; // keep it to ~15 fps at most
    this.drawMinimap();
    this.lastMinimapAt = now;
    this.minimapDirty = false;
  }

  // ── Internals ────────────────────────────────────────────────────────

  private applyView(): void {
    this.backend?.applyView({ ...this.view });
    this.reconcile();
  }

  private reconcile(): void {
    if (!this.backend || !(this.width > 0) || !(this.height > 0)) return;
    const scene = this.buildScene();
    this.backend.setScene(scene);
    const wanted: Array<{ sheetId: string; level: number; x: number; y: number }> = [];
    for (const layer of [scene.old, scene.new]) {
      if (!layer) continue;
      for (const tile of layer.tiles) wanted.push(tile);
    }
    this.loader.want(wanted);
    this.refreshScaleIndicator();
    this.minimapDirty = true;
  }

  private buildScene(): Scene {
    return {
      old: this.buildLayer('old'),
      new: this.buildLayer('new'),
    };
  }

  private buildLayer(id: 'old' | 'new'): LayerScene | null {
    const sheetId = id === 'old' ? this.oldSheetId : this.newSheetId;
    if (!sheetId) return null;
    const manifest = this.loader.getManifest(sheetId);
    if (!manifest || !(manifest.width_px > 0) || !(manifest.height_px > 0)) return null;

    const viewport: TileViewport = {
      x: this.view.x,
      y: this.view.y,
      scale: this.view.scale,
      width: this.width,
      height: this.height,
    };
    const visible = visibleTiles(sheetId, viewport, manifest, {
      ring: PREFETCH_RING,
      devicePixelRatio: this.devicePixelRatio,
    });

    const tiles: SceneTile[] = [];
    for (const tile of visible) {
      const spans = tileSpans(manifest, tile.level);
      if (!spans) continue;
      tiles.push({
        ...tile,
        rect: {
          x: tile.x * spans.spanX,
          y: tile.y * spans.spanY,
          w: spans.spanX,
          h: spans.spanY,
        },
      });
    }

    return {
      sheetId,
      manifest,
      matrix: id === 'old' ? this.oldMatrix : null,
      widthPx: manifest.width_px,
      heightPx: manifest.height_px,
      tiles,
    };
  }

  private contentBounds(): { x: number; y: number; w: number; h: number } | null {
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (const id of ['old', 'new'] as const) {
      const rect = this.layerWorldRect(id);
      if (!rect) continue;
      minX = Math.min(minX, rect.x);
      minY = Math.min(minY, rect.y);
      maxX = Math.max(maxX, rect.x + rect.w);
      maxY = Math.max(maxY, rect.y + rect.h);
    }
    if (!Number.isFinite(minX)) return null;
    return { x: minX, y: minY, w: maxX - minX, h: maxY - minY };
  }

  /** A sheet's world-space axis-aligned box (the aligned old sheet may be rotated). */
  private layerWorldRect(
    id: 'old' | 'new',
  ): { x: number; y: number; w: number; h: number } | null {
    const sheetId = id === 'old' ? this.oldSheetId : this.newSheetId;
    if (!sheetId) return null;
    const manifest = this.loader.getManifest(sheetId);
    if (!manifest || !(manifest.width_px > 0) || !(manifest.height_px > 0)) return null;

    const width = manifest.width_px;
    const height = manifest.height_px;
    const matrix = id === 'old' ? this.oldMatrix : null;
    if (!matrix) return { x: 0, y: 0, w: width, h: height };

    const corners = [
      matrix.apply({ x: 0, y: 0 }),
      matrix.apply({ x: width, y: 0 }),
      matrix.apply({ x: 0, y: height }),
      matrix.apply({ x: width, y: height }),
    ];
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (const corner of corners) {
      minX = Math.min(minX, corner.x);
      minY = Math.min(minY, corner.y);
      maxX = Math.max(maxX, corner.x);
      maxY = Math.max(maxY, corner.y);
    }
    return { x: minX, y: minY, w: maxX - minX, h: maxY - minY };
  }

  private fitKey(): string {
    const dims = (id: 'old' | 'new'): string => {
      const sheetId = id === 'old' ? this.oldSheetId : this.newSheetId;
      if (!sheetId) return '-';
      const manifest = this.loader.getManifest(sheetId);
      if (!manifest) return '-';
      return `${manifest.width_px}x${manifest.height_px}`;
    };
    const matrix = this.transformMatrix?.join(',') ?? '-';
    return `${this.oldSheetId ?? '-'}|${this.newSheetId ?? '-'}|${matrix}|${dims('old')}|${dims('new')}`;
  }

  /** Fit once per content change — never on pan or zoom. */
  private maybeFit(): void {
    if (this.lastFitKey === this.fitKey()) return;
    if (!this.contentBounds()) return;
    this.fit();
    this.lastFitKey = this.fitKey();
  }

  private refreshScaleIndicator(): void {
    const percent = Math.round(this.view.scale * 100);
    let text = `${percent}%`;
    const pxPerMm = this.pxPerMm ?? (this.dpi > 0 ? this.dpi / 25.4 : 0);
    if (pxPerMm > 0 && Number.isFinite(pxPerMm)) {
      const px100 = Math.round(100 * this.view.scale * pxPerMm);
      text += ` · 100 mm ≈ ${px100} px`;
    }
    if (this.scaleElement.textContent !== text) {
      this.scaleElement.textContent = text;
    }
  }

  // ── Minimap ──────────────────────────────────────────────────────────

  private drawMinimap(): void {
    const canvas = this.minimapCanvas;
    if (!canvas || canvas.clientWidth === 0) return;
    const cssWidth = canvas.clientWidth;
    const cssHeight = canvas.clientHeight;
    const bufferKey = `${Math.round(cssWidth * this.devicePixelRatio)}x${Math.round(
      cssHeight * this.devicePixelRatio,
    )}`;
    if (bufferKey !== this.minimapBufferKey) {
      canvas.width = Math.round(cssWidth * this.devicePixelRatio);
      canvas.height = Math.round(cssHeight * this.devicePixelRatio);
      this.minimapBufferKey = bufferKey;
    }
    const context = canvas.getContext('2d');
    if (!context) return;

    // The buffer is device-pixel sized; drawing stays in CSS pixels so the
    // numbers here match the CSS dimensions of the minimap element.
    context.setTransform(this.devicePixelRatio, 0, 0, this.devicePixelRatio, 0, 0);
    context.clearRect(0, 0, cssWidth, cssHeight);

    const bounds = this.contentBounds();
    if (!bounds || bounds.w <= 0 || bounds.h <= 0) return;

    const mapScale = Math.min(cssWidth / bounds.w, cssHeight / bounds.h);
    const offsetX = (cssWidth - bounds.w * mapScale) / 2;
    const offsetY = (cssHeight - bounds.h * mapScale) / 2;
    const project = (wx: number, wy: number): { x: number; y: number } => ({
      x: offsetX + (wx - bounds.x) * mapScale,
      y: offsetY + (wy - bounds.y) * mapScale,
    });

    // Old sheet first, new sheet on top — like the layers themselves.
    context.fillStyle = this.colors.sheet;
    this.fillWorldQuad(context, 'old', project);
    this.fillWorldQuad(context, 'new', project);

    // The viewport rectangle, in the room text colour.
    const viewScale = this.view.scale;
    const a = project(this.view.x, this.view.y);
    const b = project(
      this.view.x + this.width / viewScale,
      this.view.y + this.height / viewScale,
    );
    context.strokeStyle = this.colors.textHi;
    context.lineWidth = 1;
    context.strokeRect(a.x, a.y, b.x - a.x, b.y - a.y);
  }

  private fillWorldQuad(
    context: CanvasRenderingContext2D,
    id: 'old' | 'new',
    project: (wx: number, wy: number) => { x: number; y: number },
  ): void {
    const sheetId = id === 'old' ? this.oldSheetId : this.newSheetId;
    if (!sheetId) return;
    const manifest = this.loader.getManifest(sheetId);
    if (!manifest || !(manifest.width_px > 0) || !(manifest.height_px > 0)) return;
    const width = manifest.width_px;
    const height = manifest.height_px;

    const matrix = id === 'old' ? this.oldMatrix : null;
    const corners =
      matrix === null
        ? [
            { x: 0, y: 0 },
            { x: width, y: 0 },
            { x: width, y: height },
            { x: 0, y: height },
          ]
        : [
            matrix.apply({ x: 0, y: 0 }),
            matrix.apply({ x: width, y: 0 }),
            matrix.apply({ x: width, y: height }),
            matrix.apply({ x: 0, y: height }),
          ];

    context.beginPath();
    const first = project(corners[0]?.x ?? 0, corners[0]?.y ?? 0);
    context.moveTo(first.x, first.y);
    for (let index = 1; index < 4; index++) {
      const corner = corners[index];
      if (!corner) continue;
      const point = project(corner.x, corner.y);
      context.lineTo(point.x, point.y);
    }
    context.closePath();
    context.fill();
  }
}

// ═══════════════════════════════════════════════════════════════════════
// The React component
// ═══════════════════════════════════════════════════════════════════════

export function Lightbox(props: LightboxProps) {
  const {
    oldSheetId,
    newSheetId,
    className,
    transformMatrix = null,
    dpi = 200,
    pxPerMm = null,
  } = props;

  const loader = useTileLoader();
  const rootRef = useRef<HTMLDivElement | null>(null);
  const stageRef = useRef<HTMLDivElement | null>(null);
  const minimapRef = useRef<HTMLCanvasElement | null>(null);
  const scaleRef = useRef<HTMLDivElement | null>(null);
  const coreRef = useRef<LightboxCore | null>(null);
  const pointersRef = useRef(new Map<number, { x: number; y: number }>());
  const [rendererMode, setRendererMode] = useState<LightboxRendererMode | null>(null);

  // One effect owns the whole lifecycle; StrictMode's double mount just
  // destroys and recreates everything (loader.destroy() is a full reset).
  useEffect(() => {
    const stage = stageRef.current;
    const minimap = minimapRef.current;
    const scaleElement = scaleRef.current;
    if (!stage || !minimap || !scaleElement) return;

    const core = new LightboxCore({
      loader,
      stage,
      minimapCanvas: minimap,
      scaleElement,
      colors: readTokenColors(),
      onMode: (mode) => setRendererMode(mode),
    });
    coreRef.current = core;
    void core.start();

    return () => {
      coreRef.current = null;
      core.destroy();
    };
  }, [loader]);

  // Content changes are diffed inside the core, so re-renders that pass the
  // same sheet ids or an equal matrix are cheap no-ops.
  useEffect(() => {
    coreRef.current?.setContent({ oldSheetId, newSheetId, transformMatrix });
  }, [oldSheetId, newSheetId, transformMatrix]);

  useEffect(() => {
    coreRef.current?.setViewMeta({ dpi, pxPerMm });
  }, [dpi, pxPerMm]);

  // Wheel must be a native non-passive listener: React's synthetic wheel is
  // passive, and zooming has to preventDefault (browser page zoom).
  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    const onWheel = (event: WheelEvent): void => {
      event.preventDefault();
      const core = coreRef.current;
      if (!core) return;
      const rect = root.getBoundingClientRect();
      const sx = event.clientX - rect.left;
      const sy = event.clientY - rect.top;
      const deltaY = event.deltaMode === 1 ? event.deltaY * 33 : event.deltaY;
      core.zoomAt(sx, sy, Math.exp(-deltaY * WHEEL_ZOOM_RATE));
    };
    root.addEventListener('wheel', onWheel, { passive: false });
    return () => root.removeEventListener('wheel', onWheel);
  }, []);

  const onPointerDown = (event: React.PointerEvent<HTMLDivElement>): void => {
    if (event.pointerType === 'mouse' && event.button !== 0) return;
    const root = rootRef.current;
    if (root) {
      try {
        root.setPointerCapture(event.pointerId);
      } catch {
        // The pointer may already be gone; dragging still works without it.
      }
    }
    pointersRef.current.set(event.pointerId, {
      x: event.clientX,
      y: event.clientY,
    });
    if (stageRef.current) stageRef.current.style.cursor = 'grabbing';
  };

  const onPointerMove = (event: React.PointerEvent<HTMLDivElement>): void => {
    const pointers = pointersRef.current;
    const previous = pointers.get(event.pointerId);
    const core = coreRef.current;
    if (!previous || !core) return;

    if (pointers.size === 1) {
      // Drag to pan: the drawing follows the pointer.
      core.panByScreen(event.clientX - previous.x, event.clientY - previous.y);
    } else if (pointers.size >= 2) {
      // Pinch: keep the world point under the old midpoint under the new
      // midpoint, scaling by the change in finger distance. zoomAt works in
      // stage-local pixels, so client coordinates are converted first.
      const rect = rootRef.current?.getBoundingClientRect();
      const originX = rect?.left ?? 0;
      const originY = rect?.top ?? 0;
      const others = [...pointers.values()];
      const other = others.find((point) => point !== previous) ?? others[0];
      if (other) {
        const prevMid = {
          x: (previous.x + other.x) / 2 - originX,
          y: (previous.y + other.y) / 2 - originY,
        };
        const prevDistance = Math.hypot(other.x - previous.x, other.y - previous.y);
        if (prevDistance > 0) {
          const nextMid = {
            x: (event.clientX + other.x) / 2 - originX,
            y: (event.clientY + other.y) / 2 - originY,
          };
          const nextDistance = Math.hypot(other.x - event.clientX, other.y - event.clientY);
          core.zoomAt(prevMid.x, prevMid.y, nextDistance / prevDistance);
          // Local and client midpoints differ by a constant offset, so the
          // delta between them is already in stage pixels.
          core.panByScreen(nextMid.x - prevMid.x, nextMid.y - prevMid.y);
        }
      }
    }

    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
  };

  const releasePointer = (event: React.PointerEvent<HTMLDivElement>): void => {
    pointersRef.current.delete(event.pointerId);
    if (pointersRef.current.size === 0 && stageRef.current) {
      stageRef.current.style.cursor = 'grab';
    }
  };

  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>): void => {
    const target = event.target as HTMLElement;
    if (target.closest('input, textarea, select, button, [contenteditable="true"]')) return;
    const core = coreRef.current;
    if (!core) return;

    // Arrows pan like scrollbars: the view moves in the arrow's direction.
    let handled = true;
    switch (event.key) {
      case 'ArrowLeft':
        core.panByScreen(KEY_PAN_PX, 0);
        break;
      case 'ArrowRight':
        core.panByScreen(-KEY_PAN_PX, 0);
        break;
      case 'ArrowUp':
        core.panByScreen(0, KEY_PAN_PX);
        break;
      case 'ArrowDown':
        core.panByScreen(0, -KEY_PAN_PX);
        break;
      case '+':
      case '=':
        core.zoomAtCenter(KEY_ZOOM_FACTOR);
        break;
      case '-':
      case '_':
        core.zoomAtCenter(1 / KEY_ZOOM_FACTOR);
        break;
      case '0':
        core.fit();
        break;
      case '1':
        core.zoom100();
        break;
      default:
        handled = false;
    }
    if (handled) event.preventDefault();
  };

  const rootClassName = className ? `lightbox ${className}` : 'lightbox';

  return (
    <div
      ref={rootRef}
      className={rootClassName}
      tabIndex={0}
      aria-label="Drawing lightbox — old and new sheets overlaid"
      aria-description="Drag to pan, scroll to zoom, pinch on touch. Arrow keys pan, + and − zoom, 0 fits the sheet, 1 shows 100 percent."
      onKeyDown={onKeyDown}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={releasePointer}
      onPointerCancel={releasePointer}
    >
      <div ref={stageRef} className="lightbox__stage" />
      <canvas ref={minimapRef} className="lightbox__minimap" aria-hidden="true" />
      <div ref={scaleRef} className="lightbox__scale tabular" aria-hidden="true" />
      {rendererMode === 'canvas2d' && (
        <p className="lightbox__fallback-note" role="status">
          WebGL is not available, so this viewer is running in canvas mode.
          Pan and zoom still work; very large sheets may be slower.
        </p>
      )}
    </div>
  );
}
