/**
 * Lightbox viewer kit (Phase 4, Tasks 4.11 + 4.12).
 *
 * `Lightbox` is the tiled drawing viewer component; the supporting pieces —
 * the tile loader, the alignment→Pixi matrix bridge and the diff-palette
 * filter kit — are exported here too so later tasks (manual alignment,
 * review screens) can reuse them without importing from the component file.
 */

export { Lightbox } from './Lightbox';
export type {
  LightboxDrawEffects,
  LightboxMode,
  LightboxModeOptions,
  LightboxProps,
  LightboxRendererMode,
  LightboxSingleSide,
} from './Lightbox';
export { pixiMatrixFromAlignment } from './pixiMatrix';
export {
  COINCIDENT_INK_ALPHA,
  COLOUR_BLIND_DIFF_PALETTE,
  DEFAULT_DIFF_PALETTE,
  hexToRgb01,
} from './filters';
export type { DiffPalette, Rgb01 } from './filters';
export {
  createTileLoader,
  useTileLoader,
  visibleTiles,
  pickTileLevel,
  tileSpans,
  tileKey,
  DEFAULT_TILE_BUDGET,
  DEFAULT_CONCURRENCY,
  DEFAULT_PREFETCH_RING,
} from './useTileLoader';
export type {
  TileLoader,
  TileLoaderOptions,
  TileImage,
  TileRef,
  TileViewport,
  VisibleTile,
  VisibleTilesOptions,
} from './useTileLoader';
