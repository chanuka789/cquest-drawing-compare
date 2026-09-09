/**
 * Lightbox — the tiled drawing viewer core (Phase 4, Tasks 4.11 + 4.12).
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
 * View modes (Task 4.12), driven by the `mode` prop and the control bar:
 *  - overlay (default): both sheets recoloured — old-only ink blue
 *    `#0E63C4`, new-only ink red `#E8442A`, coinciding ink 55 % grey — via
 *    a custom fragment shader compositing two off-screen bakes
 *    (see filters.ts for the exact model and its documented
 *    approximations). The Canvas 2D fallback mirrors the same per-pixel
 *    math on a low-resolution blend canvas.
 *  - swipe: a draggable divider; the old sheet left of the seam, the new
 *    sheet right of it. The divider is a brand-red chrome line (the seam).
 *  - blink: alternates old/new at an adjustable interval (default 600 ms);
 *    Space toggles playback.
 *  - single: one sheet at a time; O/N switch between old and new.
 * The user's last mode is remembered in localStorage under
 * `cqdc.lightbox.mode` (see prefs.ts).
 *
 * Interaction (all of it moves the container transform only — textures are
 * never re-created while panning):
 *  - drag to pan, wheel zooms around the pointer, pinch on touch
 *  - keyboard: arrows pan (nudge the divider in swipe), +/− zoom, 0 fits,
 *    Ctrl+1 = 100 %, 1–4 choose the mode, Space plays/pauses blink,
 *    [ and ] fade the old layer, O/N switch the single sheet.
 *
 * Chrome drawn over the room background, never over the drawing:
 *  - a minimap (plain 2D canvas in the corner) redrawn at ~15 fps
 *  - a scale readout: zoom percent, and — from `pxPerMm` (falling back to
 *    `dpi / 25.4`) — what 100 mm on paper measures at the current zoom.
 *  - an optional control bar with the mode switches, the overlay opacity
 *    slider, the blink speed and playback controls and a reset-view
 *    button. The "show annotations layer" toggle of the plan is omitted
 *    on purpose: the engine does not serve an annotations layer yet.
 *
 * Lifecycle is one effect: it creates everything and tears everything down
 * (Pixi app, textures, observers, listeners). Cleanup is idempotent, which
 * makes React 19 StrictMode's double-mount safe.
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Application,
  Container,
  Graphics,
  ImageSource,
  Matrix,
  RenderTexture,
  Sprite,
  Texture,
  isWebGLSupported,
} from 'pixi.js';
import type { TileManifest } from '../../api/types';
import {
  COLOUR_BLIND_DIFF_PALETTE,
  DEFAULT_DIFF_PALETTE,
  LayerCompareFilter,
  compositeOverlayPixel,
  hexToRgb01,
} from './filters';
import type { DiffPalette, LayerCompareSettings, Rgb01 } from './filters';
import { pixiMatrixFromAlignment } from './pixiMatrix';
import { readStoredLightboxMode, writeStoredLightboxMode } from './prefs';
import { tileSpans, useTileLoader, visibleTiles } from './useTileLoader';
import type { TileLoader, TileViewport, VisibleTile } from './useTileLoader';

import './Lightbox.css';

export type LightboxRendererMode = 'webgl' | 'canvas2d';

/** The four view modes of Task 4.12. */
export type LightboxMode = 'overlay' | 'swipe' | 'blink' | 'single';

/** Which single sheet the single mode shows. */
export type LightboxSingleSide = 'old' | 'new';

/**
 * Initial values for the view-mode controls. Applied once, on mount (like
 * `initialMode`), and only when the matching prop is undefined — the
 * sliders and toggles are otherwise owned by the component itself.
 */
export interface LightboxModeOptions {
  /** Blink half-cycle interval in milliseconds (clamped 200–2000). */
  blinkMs?: number;
  /** Old-layer opacity for overlay (0..1). */
  oldOpacity?: number;
  /** Start with the colour-blind-safe (blue/orange) overlay palette. */
  colourBlindSafe?: boolean;
  /** Which sheet the single mode starts on. */
  singleSide?: LightboxSingleSide;
}

/** Everything the backends need to know to draw one frame's mode. */
export interface LightboxDrawEffects {
  mode: LightboxMode;
  /** 0..1 strength of the old-only ink in overlay. */
  oldOpacity: number;
  /** Which layer blink currently shows. */
  blinkShowOld: boolean;
  /** Which layer the single mode shows. */
  singleSide: LightboxSingleSide;
  /** 0..1 across the canvas: the swipe divider's position. */
  dividerFrac: number;
  /** Ink colours the overlay classification recolours with. */
  palette: DiffPalette;
}

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
   * Legacy Task 4.11 mode placeholder: `overlay` (the default), or a
   * single sheet shown raw. Kept for backward compatibility; prefer
   * `mode` + `modeOptions.singleSide`. `initialMode` only sets the initial
   * state and loses to a stored preference.
   */
  initialMode?: 'overlay' | 'single-old' | 'single-new';
  /** Extra class for the root element, e.g. a screen's layout class. */
  className?: string;
  /**
   * Controlled view mode. When provided, the component renders this mode
   * and reports user changes through `onModeChange` instead of changing
   * it internally. When absent the component owns the mode and remembers
   * it in localStorage (`cqdc.lightbox.mode`).
   */
  mode?: LightboxMode;
  /** Fired when the user picks a mode from the bar or the keyboard. */
  onModeChange?: (mode: LightboxMode) => void;
  /** Initial control values; see {@link LightboxModeOptions}. */
  modeOptions?: LightboxModeOptions;
  /**
   * Show the control bar (mode switches, sliders, reset view). Screens
   * that embed the lightbox with their own chrome pass `false`.
   * Defaults to true. Keyboard access works either way.
   */
  controls?: boolean;
}

/** Order matches the digit keys 1–4. */
const MODE_ORDER: ReadonlyArray<LightboxMode> = ['overlay', 'swipe', 'blink', 'single'];

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
/** One arrow-key press moves the swipe divider this many pixels. */
const KEY_DIVIDER_NUDGE_PX = 24;
/** Keyboard zoom factor per press. */
const KEY_ZOOM_FACTOR = 1.25;
/** Minimap redraw throttle in frames (60 fps → ~15 fps). */
const MINIMAP_EVERY_FRAMES = 4;
/** Placeholder (tile still loading) alpha over the sheet. */
const PLACEHOLDER_ALPHA = 0.55;

/** Blink interval range and default, in milliseconds. */
const BLINK_MIN_MS = 200;
const BLINK_MAX_MS = 2000;
const BLINK_DEFAULT_MS = 600;
/** Keyboard step for the old-layer opacity and its range. */
const OPACITY_KEY_STEP = 0.1;

/** Composite bakes are capped at 1.5× device pixels to bound GPU memory. */
const COMPOSITE_MAX_RESOLUTION = 1.5;
/** Canvas 2D overlay classifies at ≤ half resolution (see drawOverlay). */
const CANVAS_OVERLAY_MAX_SCALE = 0.5;

/** Initial effects until React pushes the first real ones. */
const DEFAULT_DRAW_EFFECTS: LightboxDrawEffects = {
  mode: 'overlay',
  oldOpacity: 1,
  blinkShowOld: true,
  singleSide: 'old',
  dividerFrac: 0.5,
  palette: DEFAULT_DIFF_PALETTE,
};

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
  /** Mode controls changed (mode, opacity, blink side, divider, palette). */
  setEffects(effects: LightboxDrawEffects): void;
}

const DEFAULT_COLORS: TokenColors = {
  room: '#14181c',
  sheet: '#f7f6f3',
  sheetDim: '#e8e6e1',
  textMid: '#9da9b5',
  textHi: '#edf1f5',
};

/** The sheet token as 0..1 components; mirrors `--sheet` when unreadable. */
const FALLBACK_PAGE_COLOUR: Rgb01 = { r: 0xf7 / 255, g: 0xf6 / 255, b: 0xf3 / 255 };

/** Parse a token's hex string; a malformed token falls back to `--sheet`. */
function pageColourFromToken(tokenValue: string): Rgb01 {
  try {
    return hexToRgb01(tokenValue);
  } catch {
    return FALLBACK_PAGE_COLOUR;
  }
}

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
  /** The alignment matrix applied to this container (null = identity). */
  matrix: Matrix | null;
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
    matrix: null,
    matrixKey: '',
    backdropKey: '',
    placeholderKey: '',
  };
}

/** The world-space matrix of a view, matching the world container. */
function viewMatrixFor(view: ViewState): Matrix {
  return new Matrix(view.scale, 0, 0, view.scale, -view.x * view.scale, -view.y * view.scale);
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

  /** The mode effects; DEFAULT_DRAW_EFFECTS until React pushes the first. */
  private effects: LightboxDrawEffects = DEFAULT_DRAW_EFFECTS;
  /** The sheet token colour the overlay classification paints with. */
  private readonly pageColour: Rgb01;
  /** Which layers have sheet content right now (drives visibility/bakes). */
  private hasLayer: Record<'old' | 'new', boolean> = { old: false, new: false };
  private view: ViewState = { x: 0, y: 0, scale: 1 };
  private cssWidth = 0;
  private cssHeight = 0;
  /** Bake resolution in physical pixels per CSS pixel (capped). */
  private compositeResolution = 1;

  /** Overlay/swipe composite: bake targets + full-canvas filtered sprite. */
  private rtOld: RenderTexture | null = null;
  private rtNew: RenderTexture | null = null;
  private compositeSprite: Sprite | null = null;
  private compositeFilter: LayerCompareFilter | null = null;
  private transparentTexture: Texture | null = null;
  /** Size key the current bake targets were created at. */
  private compositeTargetKey = '';
  /** Rendered into a bake target when a layer has no sheet (clears it). */
  private readonly emptyBake = new Container();

  constructor(host: FrameHost) {
    this.host = host;
    this.pageColour = pageColourFromToken(host.colors.sheet);
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
    // The composite sprite sits above the world. It is transparent and
    // normally invisible; in overlay/swipe modes the world is hidden and
    // this sprite carries the LayerCompareFilter output instead.
    this.transparentTexture = this.createTransparentTexture();
    const composite = new Sprite(this.transparentTexture);
    composite.visible = false;
    this.compositeSprite = composite;
    app.stage.addChild(composite);
    app.canvas.className = 'lightbox__pixi-canvas';
    stage.appendChild(app.canvas);
    app.ticker.add(() => this.host.onFrame());
  }

  private createTransparentTexture(): Texture {
    const blank = document.createElement('canvas');
    blank.width = 1;
    blank.height = 1;
    const source = new ImageSource({
      resource: blank,
      label: 'lightbox-composite-blank',
      autoGarbageCollect: false,
    });
    return new Texture({ source });
  }

  destroy(): void {
    if (this.destroyed) return;
    this.destroyed = true;
    this.releaseCompositeTargets();
    if (this.transparentTexture) {
      this.transparentTexture.destroy(true);
      this.transparentTexture = null;
    }
    this.compositeSprite = null;
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
    this.cssWidth = width;
    this.cssHeight = height;
    this.compositeResolution = Math.max(
      1,
      Math.min(devicePixelRatio || 1, COMPOSITE_MAX_RESOLUTION),
    );
    if (this.isComposite()) {
      const targetKey = `${width}x${height}@${this.compositeResolution}`;
      if (targetKey !== this.compositeTargetKey) {
        this.releaseCompositeTargets();
        this.ensureCompositeTargets();
      }
      // The following reconcile (setScene) re-bakes the new targets.
    }
  }

  applyView(view: ViewState): void {
    const world = this.world;
    if (!world || !(view.scale > 0)) return;
    this.view = { ...view };
    world.position.set(-view.x * view.scale, -view.y * view.scale);
    world.scale.set(view.scale, view.scale);
    // Bakes happen in setScene (the core reconciles after every view
    // change), so a composite frame is always freshly baked — no extra
    // render work here.
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
        this.hasLayer[id] = false;
        continue;
      }
      this.hasLayer[id] = true;
      this.syncLayer(layer, objects);
    }
    this.refreshVis();
    if (this.isComposite()) this.bakeComposite();
    this.evictTextures(wanted);
  }

  setEffects(effects: LightboxDrawEffects): void {
    const wasComposite = this.isComposite();
    this.effects = effects;
    const isComposite = this.isComposite();
    if (isComposite) {
      this.ensureCompositeTargets();
      const filter = this.compositeFilter;
      if (filter) filter.setSettings(this.compositeSettings());
      // Content baked while the previous mode drew directly may be stale:
      // re-bake whenever the composite pass (re)appears.
      if (!wasComposite) this.bakeComposite();
    } else {
      this.releaseCompositeTargets();
    }
    this.refreshVis();
  }

  private syncLayer(layer: LayerScene, objects: PixiLayerObjects): void {
    objects.container.visible = true;

    // The old layer carries the alignment matrix; the new one is identity.
    const matrix = layer.matrix;
    const matrixKey = matrixSignature(matrix);
    if (matrixKey !== objects.matrixKey) {
      objects.container.setFromMatrix(matrix ?? new Matrix());
      objects.matrix = matrix ?? null;
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

  // ── View-mode display (overlay/swipe composite, blink/single picks) ──

  private isComposite(): boolean {
    return this.effects.mode === 'overlay' || this.effects.mode === 'swipe';
  }

  /** Which layers the world draws when the mode is not composite. */
  private layerShownInDirectMode(id: 'old' | 'new'): boolean {
    const effects = this.effects;
    if (effects.mode === 'blink') {
      // With one sheet a blink is pointless: show it steadily.
      if (this.hasLayer.old !== this.hasLayer.new) return this.hasLayer[id];
      const wanted: 'old' | 'new' = effects.blinkShowOld ? 'old' : 'new';
      return id === wanted && this.hasLayer[id];
    }
    if (effects.mode === 'single') {
      return id === effects.singleSide && this.hasLayer[id];
    }
    return true;
  }

  /**
   * The visibility truth for one frame: composited modes hide the world
   * and show the filtered sprite; direct modes (blink/single) pick layers
   * inside the world. In composite modes the layer containers stay
   * visible because they are the bake roots — the hidden world keeps them
   * off the stage.
   */
  private refreshVis(): void {
    const world = this.world;
    const composite = this.compositeSprite;
    if (!world || !composite) return;
    const compositing = this.isComposite();
    world.visible = !compositing;
    composite.visible = compositing;
    for (const id of this.layerOrder) {
      const objects = this.layerObjects.get(id);
      if (!objects) continue;
      objects.container.visible = compositing
        ? this.hasLayer[id]
        : this.hasLayer[id] && this.layerShownInDirectMode(id);
    }
  }

  private compositeSettings(): LayerCompareSettings {
    const effects = this.effects;
    return {
      swipe: effects.mode === 'swipe',
      dividerFrac: effects.dividerFrac,
      oldOpacity: effects.oldOpacity,
      palette: effects.palette,
    };
  }

  /** Create (or recreate after a resize) the bake targets and filter. */
  private ensureCompositeTargets(): void {
    const app = this.app;
    if (!app || this.cssWidth < 1 || this.cssHeight < 1) return;
    if (this.rtOld && this.rtNew && this.compositeFilter) {
      const composite = this.compositeSprite;
      if (composite) {
        composite.width = this.cssWidth;
        composite.height = this.cssHeight;
      }
      return;
    }
    this.releaseCompositeTargets();
    const options = {
      width: this.cssWidth,
      height: this.cssHeight,
      resolution: this.compositeResolution,
      antialias: true,
    };
    const rtOld = RenderTexture.create(options);
    const rtNew = RenderTexture.create(options);
    this.rtOld = rtOld;
    this.rtNew = rtNew;
    this.compositeTargetKey = `${this.cssWidth}x${this.cssHeight}@${this.compositeResolution}`;
    const filter = new LayerCompareFilter(rtOld.source, rtNew.source, this.pageColour);
    filter.setSettings(this.compositeSettings());
    this.compositeFilter = filter;
    const composite = this.compositeSprite;
    if (composite) {
      composite.width = this.cssWidth;
      composite.height = this.cssHeight;
      composite.filters = [filter.filter];
    }
  }

  private releaseCompositeTargets(): void {
    const composite = this.compositeSprite;
    if (composite) composite.filters = [];
    this.compositeFilter?.destroy();
    this.compositeFilter = null;
    this.rtOld?.destroy(true);
    this.rtOld = null;
    this.rtNew?.destroy(true);
    this.rtNew = null;
    this.compositeTargetKey = '';
  }

  /**
   * Re-render both layers into their bake targets at the current view.
   * Runs on every view/content change while a composite mode is active,
   * so the composite sprite always reflects the latest state.
   */
  private bakeComposite(): void {
    const app = this.app;
    const rtOld = this.rtOld;
    const rtNew = this.rtNew;
    if (!app || !rtOld || !rtNew || this.cssWidth < 1 || this.cssHeight < 1) return;

    const renderer = app.renderer;
    const viewMatrix = viewMatrixFor(this.view);
    const clear = { clear: true, clearColor: [0, 0, 0, 0] };
    const oldObjects = this.layerObjects.get('old');
    const newObjects = this.layerObjects.get('new');
    const oldAlignment = oldObjects?.matrix ?? null;
    // render() replaces the root container's own transform with the one
    // given, so the alignment matrix must be folded in here: sheet pixels
    // map through alignment first, then through the view.
    const oldTransform = oldAlignment === null ? viewMatrix : viewMatrix.clone().append(oldAlignment);
    renderer.render({
      container: oldObjects && this.hasLayer.old ? oldObjects.container : this.emptyBake,
      target: rtOld,
      transform: oldTransform,
      ...clear,
    });
    renderer.render({
      container: newObjects && this.hasLayer.new ? newObjects.container : this.emptyBake,
      target: rtNew,
      transform: viewMatrix,
      ...clear,
    });
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
  /** Mode effects; DEFAULT_DRAW_EFFECTS until React pushes the first. */
  private effects: LightboxDrawEffects = DEFAULT_DRAW_EFFECTS;
  /** Reusable low-resolution overlay scratch canvases, keyed by size. */
  private scratchKey = '';
  private scratchA: HTMLCanvasElement | null = null;
  private scratchB: HTMLCanvasElement | null = null;
  private scratchOut: HTMLCanvasElement | null = null;
  private scratchAContext: CanvasRenderingContext2D | null = null;
  private scratchBContext: CanvasRenderingContext2D | null = null;
  private scratchOutContext: CanvasRenderingContext2D | null = null;
  /** The sheet token colour the overlay classification paints with. */
  private readonly pageColour: Rgb01;

  constructor(host: FrameHost) {
    this.host = host;
    this.pageColour = pageColourFromToken(host.colors.sheet);
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
    context.imageSmoothingQuality = 'high';
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
    this.scratchA = null;
    this.scratchB = null;
    this.scratchOut = null;
    this.scratchAContext = null;
    this.scratchBContext = null;
    this.scratchOutContext = null;
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

  setEffects(effects: LightboxDrawEffects): void {
    this.effects = effects;
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
    const scene = this.scene;
    if (!(view.scale > 0)) return;
    if (!scene.old && !scene.new) return;

    const effects = this.effects;
    if (effects.mode === 'overlay') {
      this.drawOverlay(context, view, scene, effects);
    } else if (effects.mode === 'swipe') {
      this.drawSwipe(context, view, scene, effects);
    } else {
      this.drawDirect(context, view, scene, effects);
    }
  }

  /** Apply the view transform to a context whose base scale is set. */
  private applyViewTransform(
    context: CanvasRenderingContext2D,
    view: ViewState,
  ): void {
    context.transform(view.scale, 0, 0, view.scale, -view.x * view.scale, -view.y * view.scale);
  }

  /**
   * Blink and single draw the raw sheets straight to the screen, exactly
   * like the 4.11 core did — only the layer choice differs. Blink with a
   * single sheet shows it steadily (nothing to alternate against).
   */
  private drawDirect(
    context: CanvasRenderingContext2D,
    view: ViewState,
    scene: Scene,
    effects: LightboxDrawEffects,
  ): void {
    let showOld = true;
    let showNew = true;
    if (effects.mode === 'blink') {
      if (scene.old && scene.new) {
        showOld = effects.blinkShowOld;
        showNew = !effects.blinkShowOld;
      } else {
        showOld = Boolean(scene.old);
        showNew = Boolean(scene.new);
      }
    } else if (effects.mode === 'single') {
      showOld = effects.singleSide === 'old';
      showNew = effects.singleSide === 'new';
    }

    context.save();
    this.applyViewTransform(context, view);
    if (showOld) this.drawLayer(context, scene.old);
    if (showNew) this.drawLayer(context, scene.new);
    context.restore();
  }

  /**
   * Swipe draws each raw sheet clipped to its own side of the divider —
   * a native canvas clip, the 2D equivalent of the shader's `uSwipe` cut.
   * The brand-red divider line itself is chrome drawn by the component.
   */
  private drawSwipe(
    context: CanvasRenderingContext2D,
    view: ViewState,
    scene: Scene,
    effects: LightboxDrawEffects,
  ): void {
    const dividerX = Math.min(this.width, Math.max(0, effects.dividerFrac * this.width));
    context.save();
    if (scene.old) {
      context.save();
      context.beginPath();
      context.rect(0, 0, dividerX, this.height);
      context.clip();
      this.applyViewTransform(context, view);
      this.drawLayer(context, scene.old);
      context.restore();
    }
    if (scene.new) {
      context.save();
      context.beginPath();
      context.rect(dividerX, 0, this.width - dividerX, this.height);
      context.clip();
      this.applyViewTransform(context, view);
      this.drawLayer(context, scene.new);
      context.restore();
    }
    context.restore();
  }

  /**
   * Overlay, the only per-pixel mode on this path. Both layers are baked
   * into low-resolution scratch canvases (scale ≤ 0.5 — classification is
   * a colour decision, not a geometry one), then every pixel is classified
   * with the same math the WebGL shader runs (`compositeOverlayPixel`),
   * and the result is upscaled back onto the stage. This mirrors the
   * shader semantics; the reduced resolution is the documented
   * approximation of the fallback path.
   */
  private drawOverlay(
    context: CanvasRenderingContext2D,
    view: ViewState,
    scene: Scene,
    effects: LightboxDrawEffects,
  ): void {
    const largest = Math.max(this.width, this.height);
    const scale = Math.min(
      CANVAS_OVERLAY_MAX_SCALE,
      Math.max(0.25, 560 / Math.max(1, largest)),
    );
    const width = Math.max(1, Math.round(this.width * scale));
    const height = Math.max(1, Math.round(this.height * scale));
    this.ensureScratch(width, height);
    if (!this.scratchA || !this.scratchB || !this.scratchOut) return;
    const bakeA = this.scratchAContext;
    const bakeB = this.scratchBContext;
    const outContext = this.scratchOutContext;
    if (!bakeA || !bakeB || !outContext) return;

    this.bakeLayerCanvas(bakeA, width, height, scene.old, view, scale);
    this.bakeLayerCanvas(bakeB, width, height, scene.new, view, scale);

    const dataA = bakeA.getImageData(0, 0, width, height);
    const dataB = bakeB.getImageData(0, 0, width, height);
    const result = outContext.createImageData(width, height);
    const a = dataA.data;
    const b = dataB.data;
    const out = result.data;
    for (let i = 0; i < out.length; i += 4) {
      const pixel = compositeOverlayPixel(
        [a[i]!, a[i + 1]!, a[i + 2]!, a[i + 3]!],
        [b[i]!, b[i + 1]!, b[i + 2]!, b[i + 3]!],
        effects.oldOpacity,
        effects.palette,
        this.pageColour,
      );
      // alpha 0: no sheet here — leave the pixel transparent so the room
      // background shows through the upscaled result.
      if (pixel[3] > 0) {
        out[i] = pixel[0];
        out[i + 1] = pixel[1];
        out[i + 2] = pixel[2];
        out[i + 3] = pixel[3];
      }
    }
    outContext.putImageData(result, 0, 0);
    context.drawImage(this.scratchOut, 0, 0, width, height, 0, 0, this.width, this.height);
  }

  /** Render one layer (or clear the scratch) at `scale` CSS pixels. */
  private bakeLayerCanvas(
    targetContext: CanvasRenderingContext2D,
    width: number,
    height: number,
    layer: LayerScene | null,
    view: ViewState,
    scale: number,
  ): void {
    targetContext.setTransform(1, 0, 0, 1, 0, 0);
    targetContext.clearRect(0, 0, width, height);
    if (!layer) return;
    targetContext.setTransform(scale, 0, 0, scale, 0, 0);
    this.applyViewTransform(targetContext, view);
    this.drawLayer(targetContext, layer);
  }

  /** (Re)allocate the three overlay scratch canvases when their size changes. */
  private ensureScratch(width: number, height: number): void {
    const key = `${width}x${height}`;
    if (key === this.scratchKey && this.scratchA) return;
    this.scratchKey = key;
    this.scratchA = this.makeScratch('lightbox__overlay-bake-a', width, height);
    this.scratchB = this.makeScratch('lightbox__overlay-bake-b', width, height);
    this.scratchOut = this.makeScratch('lightbox__overlay-out', width, height);
    this.scratchAContext = this.scratchA.getContext('2d', { willReadFrequently: true });
    this.scratchBContext = this.scratchB.getContext('2d', { willReadFrequently: true });
    this.scratchOutContext = this.scratchOut.getContext('2d');
    if (!this.scratchAContext || !this.scratchBContext || !this.scratchOutContext) {
      this.scratchA = null;
      this.scratchB = null;
      this.scratchOut = null;
      this.scratchAContext = null;
      this.scratchBContext = null;
      this.scratchOutContext = null;
    }
  }

  private makeScratch(className: string, width: number, height: number): HTMLCanvasElement {
    const scratch = document.createElement('canvas');
    scratch.className = className;
    scratch.width = width;
    scratch.height = height;
    return scratch;
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
  /** Last mode effects; forwarded to backends as they appear. */
  private effects: LightboxDrawEffects = DEFAULT_DRAW_EFFECTS;

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
    this.backend.setEffects(this.effects);

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

  /**
   * Mode/control changes from React (mode, opacity, blink side, divider,
   * palette). Backends react immediately; a backend that starts later
   * receives the stored effects in `start()`.
   */
  setDrawEffects(effects: LightboxDrawEffects): void {
    this.effects = effects;
    this.backend?.setEffects(effects);
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

function clamp01(value: number): number {
  return Math.min(1, Math.max(0, value));
}

function clampBlinkMs(value: number): number {
  return Math.min(BLINK_MAX_MS, Math.max(BLINK_MIN_MS, Math.round(value)));
}

/** Clamp a screen-pixel divider position inside the stage, off the walls. */
function clampDividerPx(value: number, width: number): number {
  if (width <= 0) return 0;
  const min = 2;
  const max = Math.max(min, width - 2);
  return Math.min(max, Math.max(min, value));
}

const MODE_LABELS: Record<LightboxMode, string> = {
  overlay: 'Overlay',
  swipe: 'Swipe',
  blink: 'Blink',
  single: 'Single',
};

const MODE_TITLES: Record<LightboxMode, string> = {
  overlay: 'Both sheets, recoloured: old blue, new red, unchanged grey',
  swipe: 'The seam: old sheet left of the divider, new sheet right',
  blink: 'Alternates between the sheets — movement shows the changes',
  single: 'One sheet at a time; O and N switch between them',
};

function modeKeyHint(mode: LightboxMode): string {
  return `${MODE_ORDER.indexOf(mode) + 1}`;
}

export function Lightbox(props: LightboxProps) {
  const {
    oldSheetId,
    newSheetId,
    className,
    transformMatrix = null,
    dpi = 200,
    pxPerMm = null,
    controls = true,
  } = props;

  const loader = useTileLoader();
  const rootRef = useRef<HTMLDivElement | null>(null);
  const canvasAreaRef = useRef<HTMLDivElement | null>(null);
  const stageRef = useRef<HTMLDivElement | null>(null);
  const minimapRef = useRef<HTMLCanvasElement | null>(null);
  const scaleRef = useRef<HTMLDivElement | null>(null);
  const coreRef = useRef<LightboxCore | null>(null);
  const pointersRef = useRef(new Map<number, { x: number; y: number }>());
  const dividerDragRef = useRef<{ pointerId: number } | null>(null);
  const [rendererMode, setRendererMode] = useState<LightboxRendererMode | null>(null);

  // ── Mode + control state ─────────────────────────────────────────────
  // The remembered mode is the one persisted preference of the app; the
  // remaining controls (opacity, blink speed, palette, single side) are
  // per-viewer state initialised from `modeOptions`.
  const [internalMode, setInternalMode] = useState<LightboxMode>(() => {
    const stored = readStoredLightboxMode();
    if (stored !== null) return stored;
    if (props.initialMode === 'single-old' || props.initialMode === 'single-new') {
      return 'single';
    }
    return 'overlay';
  });
  /** Controlled through `props.mode` when the consumer provides it. */
  const mode = props.mode ?? internalMode;

  const [oldOpacity, setOldOpacity] = useState<number>(() =>
    clamp01(props.modeOptions?.oldOpacity ?? 1),
  );
  const [blinkMs, setBlinkMs] = useState<number>(() =>
    clampBlinkMs(props.modeOptions?.blinkMs ?? BLINK_DEFAULT_MS),
  );
  const [blinkPlaying, setBlinkPlaying] = useState(true);
  const [blinkShowOld, setBlinkShowOld] = useState(true);
  const [colourBlindSafe, setColourBlindSafe] = useState<boolean>(
    () => props.modeOptions?.colourBlindSafe ?? false,
  );
  const [singleSide, setSingleSide] = useState<LightboxSingleSide>(() => {
    if (props.modeOptions?.singleSide) return props.modeOptions.singleSide;
    if (props.initialMode === 'single-old') return 'old';
    if (props.initialMode === 'single-new') return 'new';
    return 'old';
  });
  /** Swipe divider in stage pixels; null = centred. */
  const [dividerPx, setDividerPx] = useState<number | null>(null);
  const [stageSize, setStageSize] = useState({ width: 0, height: 0 });

  const chooseMode = (next: LightboxMode): void => {
    if (next === mode) return;
    writeStoredLightboxMode(next);
    if (props.onModeChange) props.onModeChange(next);
    else setInternalMode(next);
  };

  const adjustOpacity = (delta: number): void => {
    setOldOpacity((value) => clamp01(Math.round((value + delta) * 100) / 100));
  };

  // The blink timer lives here, in React: at most ~5 flips a second, so a
  // re-render per flip is cheap and the interval restarts on speed changes.
  // Blink starts on the old sheet (the initial state) and resumes from
  // wherever it was paused when the mode is re-entered.
  useEffect(() => {
    if (mode !== 'blink' || !blinkPlaying) return;
    const timer = window.setInterval(() => {
      setBlinkShowOld((showing) => !showing);
    }, blinkMs);
    return () => window.clearInterval(timer);
  }, [mode, blinkPlaying, blinkMs]);

  // The stage's CSS size, needed for the divider's centring and fractions.
  useEffect(() => {
    const area = canvasAreaRef.current;
    if (!area) return;
    const measure = (): void => {
      setStageSize({ width: area.clientWidth, height: area.clientHeight });
    };
    measure();
    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', measure);
      return () => window.removeEventListener('resize', measure);
    }
    const observer = new ResizeObserver(measure);
    observer.observe(area);
    return () => observer.disconnect();
  }, []);

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
      onMode: (renderer) => setRendererMode(renderer),
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

  // The mode effects travel as one object so backends can diff them; a
  // push happens on every control change (and on blink flips). In single
  // mode the side falls back to the sheet that actually exists.
  const effectiveSingleSide: LightboxSingleSide =
    singleSide === 'old' && !oldSheetId && newSheetId
      ? 'new'
      : singleSide === 'new' && !newSheetId && oldSheetId
        ? 'old'
        : singleSide;
  const dividerFrac =
    stageSize.width > 0
      ? clamp01((dividerPx ?? stageSize.width / 2) / stageSize.width)
      : 0.5;
  const effects = useMemo<LightboxDrawEffects>(
    () => ({
      mode,
      oldOpacity,
      blinkShowOld,
      singleSide: effectiveSingleSide,
      dividerFrac,
      palette: colourBlindSafe ? COLOUR_BLIND_DIFF_PALETTE : DEFAULT_DIFF_PALETTE,
    }),
    [
      mode,
      oldOpacity,
      blinkShowOld,
      effectiveSingleSide,
      dividerFrac,
      colourBlindSafe,
    ],
  );
  useEffect(() => {
    coreRef.current?.setDrawEffects(effects);
  }, [effects]);

  // Wheel must be a native non-passive listener: React's synthetic wheel is
  // passive, and zooming has to preventDefault (browser page zoom).
  useEffect(() => {
    const area = canvasAreaRef.current;
    if (!area) return;
    const onWheel = (event: WheelEvent): void => {
      event.preventDefault();
      const core = coreRef.current;
      if (!core) return;
      const rect = area.getBoundingClientRect();
      const sx = event.clientX - rect.left;
      const sy = event.clientY - rect.top;
      const deltaY = event.deltaMode === 1 ? event.deltaY * 33 : event.deltaY;
      core.zoomAt(sx, sy, Math.exp(-deltaY * WHEEL_ZOOM_RATE));
    };
    area.addEventListener('wheel', onWheel, { passive: false });
    return () => area.removeEventListener('wheel', onWheel);
  }, []);

  const isChromeTarget = (target: HTMLElement): boolean =>
    target.closest(
      'button, input, select, textarea, label, .lightbox__divider, .lightbox__controls',
    ) !== null;

  const onPointerDown = (event: React.PointerEvent<HTMLDivElement>): void => {
    if (event.pointerType === 'mouse' && event.button !== 0) return;
    // Buttons, sliders and the divider are chrome, not canvas.
    if (isChromeTarget(event.target as HTMLElement)) return;
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
      const rect = canvasAreaRef.current?.getBoundingClientRect();
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

  // ── Swipe divider (chrome; drags horizontally, never pans) ───────────

  const dividerLeftPx =
    stageSize.width > 0
      ? clampDividerPx(dividerPx ?? stageSize.width / 2, stageSize.width)
      : 0;
  const showDivider = mode === 'swipe' && Boolean(oldSheetId) && Boolean(newSheetId);

  const onDividerPointerDown = (event: React.PointerEvent<HTMLDivElement>): void => {
    event.stopPropagation();
    dividerDragRef.current = { pointerId: event.pointerId };
    try {
      event.currentTarget.setPointerCapture(event.pointerId);
    } catch {
      // Pointer capture is best-effort; movement still tracks while inside.
    }
  };

  const onDividerPointerMove = (event: React.PointerEvent<HTMLDivElement>): void => {
    const drag = dividerDragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const area = canvasAreaRef.current;
    if (!area || stageSize.width <= 0) return;
    const rect = area.getBoundingClientRect();
    setDividerPx(clampDividerPx(event.clientX - rect.left, stageSize.width));
  };

  const endDividerDrag = (event: React.PointerEvent<HTMLDivElement>): void => {
    if (dividerDragRef.current?.pointerId === event.pointerId) {
      dividerDragRef.current = null;
      try {
        event.currentTarget.releasePointerCapture(event.pointerId);
      } catch {
        // No capture was held — nothing to release.
      }
    }
  };

  const nudgeDivider = (delta: number): void => {
    setDividerPx((current) => {
      const width = stageSize.width;
      const anchor = current ?? (width > 0 ? width / 2 : 0);
      return clampDividerPx(anchor + delta, width);
    });
  };

  // ── Keyboard ─────────────────────────────────────────────────────────

  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>): void => {
    const target = event.target as HTMLElement;
    if (target.closest('input, textarea, select, button, [contenteditable="true"]')) return;
    const core = coreRef.current;
    if (!core) return;

    // 100 % zoom moved to Ctrl+1 — the plain digit keys now choose modes.
    if (event.ctrlKey || event.metaKey) {
      if (event.key === '1') {
        event.preventDefault();
        core.zoom100();
      }
      return;
    }

    let handled = true;
    switch (event.key) {
      case '1':
      case '2':
      case '3':
      case '4': {
        const next = MODE_ORDER[Number(event.key) - 1];
        if (next) chooseMode(next);
        break;
      }
      case ' ':
        if (mode === 'blink') setBlinkPlaying((playing) => !playing);
        break;
      case '[':
        adjustOpacity(-OPACITY_KEY_STEP);
        break;
      case ']':
        adjustOpacity(OPACITY_KEY_STEP);
        break;
      case 'o':
      case 'O':
        if (mode === 'single' && oldSheetId) setSingleSide('old');
        break;
      case 'n':
      case 'N':
        if (mode === 'single' && newSheetId) setSingleSide('new');
        break;
      case 'ArrowLeft':
        if (mode === 'swipe' && !event.shiftKey) nudgeDivider(-KEY_DIVIDER_NUDGE_PX);
        else core.panByScreen(KEY_PAN_PX, 0);
        break;
      case 'ArrowRight':
        if (mode === 'swipe' && !event.shiftKey) nudgeDivider(KEY_DIVIDER_NUDGE_PX);
        else core.panByScreen(-KEY_PAN_PX, 0);
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
      default:
        handled = false;
    }
    if (handled) event.preventDefault();
  };

  const rootClassName = className ? `lightbox ${className}` : 'lightbox';
  const digitHints = MODE_ORDER.map((value) => `${modeKeyHint(value)} ${value}`).join(' · ');
  const description =
    `Drag to pan, scroll to zoom, pinch on touch. ${digitHints} choose the mode. ` +
    'Arrows pan (in swipe they move the divider), + and − zoom, 0 fits, Ctrl+1 shows 100 percent. ' +
    'Space plays or pauses blink, [ and ] fade the old layer, O and N switch the single sheet.';

  return (
    <div
      ref={rootRef}
      className={rootClassName}
      tabIndex={0}
      aria-label="Drawing lightbox"
      aria-description={description}
      onKeyDown={onKeyDown}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={releasePointer}
      onPointerCancel={releasePointer}
    >
      <div ref={canvasAreaRef} className="lightbox__canvas-area">
        <div ref={stageRef} className="lightbox__stage" />
        <canvas ref={minimapRef} className="lightbox__minimap" aria-hidden="true" />
        <div ref={scaleRef} className="lightbox__scale tabular" aria-hidden="true" />
        {showDivider && (
          <div
            className="lightbox__divider"
            role="separator"
            aria-label="Compare divider — old sheet left, new sheet right"
            aria-orientation="vertical"
            title="Drag, or use the left and right arrow keys"
            style={{ left: `${dividerLeftPx}px` }}
            onPointerDown={onDividerPointerDown}
            onPointerMove={onDividerPointerMove}
            onPointerUp={endDividerDrag}
            onPointerCancel={endDividerDrag}
          />
        )}
        {rendererMode === 'canvas2d' && (
          <p className="lightbox__fallback-note" role="status">
            WebGL is not available, so this viewer is running in canvas mode.
            Pan and zoom still work; very large sheets may be slower.
          </p>
        )}
      </div>

      {controls && (
        <div className="lightbox__controls">
          <div
            className="lightbox__seg"
            role="group"
            aria-label="View mode"
          >
            {MODE_ORDER.map((value) => (
              <button
                key={value}
                type="button"
                className={`lightbox__mode-btn${mode === value ? ' is-active' : ''}`}
                aria-pressed={mode === value}
                aria-keyshortcuts={modeKeyHint(value)}
                title={`${MODE_TITLES[value]} — key ${modeKeyHint(value)}`}
                onClick={() => chooseMode(value)}
              >
                {MODE_LABELS[value]}
              </button>
            ))}
          </div>

          {mode === 'overlay' && (
            <label className="lightbox__field" title="Fade the old-only ink — [ and ] adjust it from the keyboard">
              <span>Old opacity</span>
              <input
                type="range"
                min={0}
                max={1}
                step={0.05}
                value={oldOpacity}
                onChange={(event) => setOldOpacity(clamp01(Number(event.target.value)))}
              />
              <span className="lightbox__field-value tabular">
                {Math.round(oldOpacity * 100)}%
              </span>
            </label>
          )}

          {mode === 'blink' && (
            <>
              <label className="lightbox__field" title="How long each sheet shows before the other replaces it">
                <span>Blink speed</span>
                <input
                  type="range"
                  min={BLINK_MIN_MS}
                  max={BLINK_MAX_MS}
                  step={50}
                  value={blinkMs}
                  onChange={(event) => setBlinkMs(clampBlinkMs(Number(event.target.value)))}
                />
                <span className="lightbox__field-value tabular">{blinkMs} ms</span>
              </label>
              <button
                type="button"
                className="lightbox__play-btn"
                aria-pressed={blinkPlaying}
                title={blinkPlaying ? 'Pause the blink — Space' : 'Play the blink — Space'}
                onClick={() => setBlinkPlaying((playing) => !playing)}
              >
                {blinkPlaying ? 'Pause blink' : 'Play blink'}
              </button>
            </>
          )}

          {mode === 'single' && (
            <div className="lightbox__seg" role="group" aria-label="Sheet shown in single mode">
              <button
                type="button"
                className={`lightbox__mode-btn${singleSide === 'old' ? ' is-active' : ''}`}
                aria-pressed={singleSide === 'old'}
                aria-keyshortcuts="o"
                title="Show the previous-issue sheet — O"
                disabled={!oldSheetId}
                onClick={() => setSingleSide('old')}
              >
                Old sheet
              </button>
              <button
                type="button"
                className={`lightbox__mode-btn${singleSide === 'new' ? ' is-active' : ''}`}
                aria-pressed={singleSide === 'new'}
                aria-keyshortcuts="n"
                title="Show the current-issue sheet — N"
                disabled={!newSheetId}
                onClick={() => setSingleSide('new')}
              >
                New sheet
              </button>
            </div>
          )}

          {mode === 'overlay' && (
            <button
              type="button"
              className="lightbox__palette-btn"
              aria-pressed={colourBlindSafe}
              title="Deuteranopia-safe palette: blue stays, red becomes orange"
              onClick={() => setColourBlindSafe((safe) => !safe)}
            >
              Colour-blind safe palette
            </button>
          )}

          <button
            type="button"
            className="lightbox__reset-btn"
            title="Fit the sheets in the view — 0"
            onClick={() => coreRef.current?.fit()}
          >
            Reset view
          </button>

          <span className="lightbox__controls-note micro">
            Annotations layer not available yet.
          </span>
        </div>
      )}
    </div>
  );
}
