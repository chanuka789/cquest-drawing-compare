/**
 * Lightbox viewer kit (Phase 4, Task 4.11).
 *
 * `Lightbox` is the tiled drawing viewer component; the supporting pieces —
 * the tile loader and the alignment→Pixi matrix bridge — are exported here
 * too so later tasks (view modes, manual alignment) can reuse them without
 * importing from the component file.
 */

export { Lightbox } from './Lightbox';
export type { LightboxProps, LightboxRendererMode } from './Lightbox';
export { pixiMatrixFromAlignment } from './pixiMatrix';
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
