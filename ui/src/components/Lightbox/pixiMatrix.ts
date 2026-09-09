/**
 * Bridging the engine's alignment matrix to the viewer's renderers.
 *
 * The engine expresses an alignment as a 3×3 row-major matrix `m` (indices
 * m[row][col]) mapping OLD sheet pixels to NEW sheet pixels, y growing
 * downwards in both images:
 *
 *   x' = m[0][0]·x + m[0][1]·y + m[0][2]
 *   y' = m[1][0]·x + m[1][1]·y + m[1][2]
 *
 * PixiJS (and the Canvas 2D `transform(a,b,c,d,tx,ty)`) store an affine
 * transform as six values with the same convention:
 *
 *   x' = a·x + c·y + tx
 *   y' = b·x + d·y + ty
 *
 * so the field mapping is a = m00, b = m10, c = m01, d = m11, tx = m02,
 * ty = m12. The old sheet is drawn inside a container carrying this matrix;
 * no Python-side resampling ever happens.
 *
 * The function is deliberately pure — no renderer state, no side effects —
 * so it can be verified by reading. There is no UI test infrastructure in
 * this repository yet.
 */

import { Matrix } from 'pixi.js';

/**
 * Convert the engine's row-major alignment matrix (old pixels → new pixels)
 * into a PixiJS affine matrix.
 *
 * `null` (or a malformed array) yields the identity: the old sheet is drawn
 * exactly in place, which is the correct pre-alignment view and also what a
 * layer without a transform uses.
 */
export function pixiMatrixFromAlignment(matrix: number[] | null): Matrix {
  if (matrix === null || matrix.length !== 9) return new Matrix();

  // The tuple cast is safe: length was checked just above.
  const [m00, m01, m02, m10, m11, m12] = matrix as [
    number,
    number,
    number,
    number,
    number,
    number,
    number,
    number,
    number,
  ];

  // A matrix of NaN or Infinity cannot render sensibly; treat it as absent.
  if (![m00, m01, m02, m10, m11, m12].every(Number.isFinite)) return new Matrix();

  // Matrix(a, b, c, d, tx, ty) — see the mapping note at the top of the file.
  return new Matrix(m00, m10, m01, m11, m02, m12);
}
