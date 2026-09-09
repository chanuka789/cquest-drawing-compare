/**
 * The lightbox's diff-compositing shader kit (Phase 4, Task 4.12).
 *
 * The four view modes reduce to two rendering problems:
 *
 *  - blink / single show one raw sheet at a time (pure layer visibility),
 *  - overlay and swipe need BOTH sheets in one pixel so that the decision
 *    can be made per fragment. Pixi cannot express "coinciding ink turns
 *    grey" with stacked sprites and blend modes (blue × red is never a
 *    neutral grey), so those two modes composite two off-screen render
 *    textures through one custom fragment shader (`LayerCompareFilter`).
 *    The old sheet is baked into `uTexA` with the view × alignment matrix
 *    and the new sheet into `uTexB` with just the view matrix, so a texture
 *    coordinate on the screen samples the same drawing point on both sides.
 *
 * ── The ink model ──────────────────────────────────────────────────────
 *
 * Sheets render as ink on a white page. Ink is estimated from luminance:
 * `presence` is the sheet's alpha in the bake (1 on the page, 0 outside it)
 * and ink is `presence × (1 − luminance)`. The renders are essentially
 * monochrome line art, so luminance is a reliable ink measure; coloured
 * drawing content (rare) is treated as ink and recoloured like everything
 * else. This is an approximation and is documented as such.
 *
 * Per pixel the shader computes:
 *
 *   both  = min(ink old, ink new)      → "unchanged" ink
 *   onlyA = ink old − both             → old-only change
 *   onlyB = ink new − both             → new-only change
 *
 *   output = the sheet page (the `--sheet` token colour), tinted:
 *     + onlyA with the old ink colour (#0E63C4 blue, diff palette)
 *     + onlyB with the new ink colour (#E8442A red, diff palette)
 *     + COINCIDENT_INK_ALPHA × both of black ink (55 % grey, mirrors the
 *       `--diff-none` token rgba(0,0,0,0.55) over the page)
 *
 * Known, accepted approximations (v1, deliberately simple):
 *  1. Coincidence is decided per pixel, so sub-pixel misalignment shows as
 *     thin blue/red fringes along stroke edges. A wider neighbourhood test
 *     would hide them at the cost of a blurrier picture.
 *  2. Both layers are always baked at the current view, so overlay/swipe
 *     re-bake while panning or zooming (two extra GPU passes per change).
 *  3. The old-layer opacity slider scales only the old-only ink (the
 *     "change" colour); unchanged 55 % grey and new-only red are the
 *     baseline and stay put. At 0 the overlay reads as "new versus
 *     unchanged".
 *  4. Drawings with real colour content are recoloured by luminance (see
 *     the ink model above).
 *
 * ── Colour-blind-safe palette ─────────────────────────────────────────
 *
 * The default pair blue #0E63C4 / red #E8442A is indistinguishable for
 * deuteranopia. The alternative keeps the blue and replaces the red with
 * the orange from the `--warn` token family (#E39A00), the standard
 * blue/orange palette. Coincident ink stays 55 % grey in both palettes.
 *
 * ── Orientation ────────────────────────────────────────────────────────
 *
 * The fragment samples both render textures at `vUv`, the raw quad
 * coordinate (0..1 across the filter area, which is the whole canvas).
 * Both textures are produced by this same renderer into render targets,
 * which share Pixi's render-texture orientation conventions (the same
 * conventions its own filters rely on for the input and `uBackTexture`
 * framebuffers). If a browser ever disagreed, the symptom would be a
 * vertical mirror of the two sheets; the Canvas 2D fallback path exists
 * and is the reference implementation of the same semantics.
 */

import { Filter, GlProgram, UniformGroup } from 'pixi.js';
import type { TextureSource } from 'pixi.js';

// ── Diff palette (mirrors the `--diff-*` tokens) ───────────────────────

/** A colour as 0..1 components, the form shader uniforms want. */
export interface Rgb01 {
  r: number;
  g: number;
  b: number;
}

/** The two inks the overlay mode recolours coincident content with. */
export interface DiffPalette {
  oldInk: Rgb01;
  newInk: Rgb01;
}

/** Token `--diff-old` #0E63C4 and `--diff-new` #E8442A. */
export const DEFAULT_DIFF_PALETTE: DiffPalette = {
  oldInk: { r: 0x0e / 255, g: 0x63 / 255, b: 0xc4 / 255 },
  newInk: { r: 0xe8 / 255, g: 0x44 / 255, b: 0x2a / 255 },
};

/** Deuteranopia-safe pair: blue stays, red becomes orange #E39A00. */
export const COLOUR_BLIND_DIFF_PALETTE: DiffPalette = {
  oldInk: { r: 0x0e / 255, g: 0x63 / 255, b: 0xc4 / 255 },
  newInk: { r: 0xe3 / 255, g: 0x9a / 255, b: 0 },
};

/**
 * The darkness of "unchanged" ink. Mirrors the token
 * `--diff-none: rgba(0, 0, 0, 0.55)`: black ink at 55 % alpha over the
 * white sheet reads as #737373. The value appears twice, here (for the
 * Canvas 2D reference math) and as a literal inside the GLSL (GPU-side);
 * they must stay equal.
 */
export const COINCIDENT_INK_ALPHA = 0.55;

/** Parse `#rrggbb` (used by the Canvas 2D reference path and tests). */
export function hexToRgb01(hex: string): Rgb01 {
  const value = hex.replace('#', '');
  const full = value.length === 3 ? value.split('').map((c) => c + c).join('') : value;
  const parsed = Number.parseInt(full, 16);
  if (!Number.isFinite(parsed) || full.length !== 6) {
    throw new Error(`Not an #rrggbb colour: ${hex}`);
  }
  return {
    r: ((parsed >> 16) & 0xff) / 255,
    g: ((parsed >> 8) & 0xff) / 255,
    b: (parsed & 0xff) / 255,
  };
}

// ── The GLSL ───────────────────────────────────────────────────────────

/**
 * Vertex shader: identical maths to Pixi's `defaultFilterVert` (the stock
 * filter vertex, MIT) with one addition — `vUv` carries the raw quad
 * coordinate, which spans 0..1 across the filter area (the whole canvas
 * for this viewer). The baked layer textures cover exactly that area, so
 * `vUv` is the sampling coordinate for both of them.
 */
export const LAYER_COMPARE_VERTEX = /* glsl */ `
in vec2 aPosition;
out vec2 vTextureCoord;
out vec2 vUv;

uniform vec4 uInputSize;
uniform vec4 uOutputFrame;
uniform vec4 uOutputTexture;

vec4 filterVertexPosition(void)
{
    vec2 position = aPosition * uOutputFrame.zw + uOutputFrame.xy;
    position.x = position.x * (2.0 / uOutputTexture.x) - 1.0;
    position.y = position.y * (2.0 * uOutputTexture.z / uOutputTexture.y) - uOutputTexture.z;
    return vec4(position, 0.0, 1.0);
}

vec2 filterTextureCoord(void)
{
    return aPosition * (uOutputFrame.zw * uInputSize.zw);
}

void main(void)
{
    gl_Position = filterVertexPosition();
    vTextureCoord = filterTextureCoord();
    vUv = aPosition;
}
`;

/**
 * Fragment shader for overlay and swipe.
 *
 * Both bakes are premultiplied-alpha render textures: alpha carries page
 * presence, so `presence − luminance(rgb)` is ink coverage exactly
 * (for premultiplied white, rgb == alpha, so the formula reads alpha minus
 * alpha × luminance of the straight colour — the page background has zero
 * ink, black ink has full ink).
 *
 * `uSwipe` selects between the two behaviours:
 *  - 0 (overlay): the blue / red / 55 % grey classification above.
 *  - 1 (swipe):   the pixel left of `uDivider` (a 0..1 screen fraction)
 *                 shows the old bake, right of it the new bake, raw.
 */
export const LAYER_COMPARE_FRAGMENT = /* glsl */ `
in vec2 vUv;
out vec4 finalColor;

uniform sampler2D uTexA;
uniform sampler2D uTexB;

uniform float uOldOpacity;
uniform vec4 uColourOld;
uniform vec4 uColourNew;
uniform vec4 uPage;
uniform float uSwipe;
uniform float uDivider;

const float COINCIDENT_ALPHA = 0.55;

float luma(vec3 colour)
{
    return dot(colour, vec3(0.299, 0.587, 0.114));
}

void main(void)
{
    vec4 oldSample = texture(uTexA, vUv);
    vec4 newSample = texture(uTexB, vUv);

    if (uSwipe > 0.5) {
        finalColor = (vUv.x < uDivider) ? oldSample : newSample;
        return;
    }

    float presenceA = oldSample.a;
    float presenceB = newSample.a;
    float inkA = clamp(presenceA - luma(oldSample.rgb), 0.0, 1.0) * uOldOpacity;
    float inkB = clamp(presenceB - luma(newSample.rgb), 0.0, 1.0);

    float both = min(inkA, inkB);
    float onlyA = inkA - both;
    float onlyB = inkB - both;

    float outAlpha = clamp(presenceA + presenceB, 0.0, 1.0);

    vec3 colour = uPage.rgb; // the warm sheet background
    colour = mix(colour, uColourOld.rgb, onlyA);
    colour = mix(colour, uColourNew.rgb, onlyB);
    colour = mix(colour, vec3(0.0), COINCIDENT_ALPHA * both);

    finalColor = vec4(colour * outAlpha, outAlpha);
}
`;

// ── The WebGL filter object ────────────────────────────────────────────

/** Everything the composite shader needs to know for one frame. */
export interface LayerCompareSettings {
  /** true → swipe cut at `dividerFrac`; false → overlay classification. */
  swipe: boolean;
  /** 0..1 across the canvas: where the swipe divider sits. */
  dividerFrac: number;
  /** 0..1; how strongly old-only ink shows (overlay only). */
  oldOpacity: number;
  /** The ink colours for old-only and new-only content (overlay only). */
  palette: DiffPalette;
}

/** Uniform layout — names must match the GLSL declarations above. */
const COMPARE_UNIFORM_STRUCT = {
  uOldOpacity: { value: 1, type: 'f32' as const },
  uColourOld: { value: [0, 0, 0, 1] as number[], type: 'vec4<f32>' as const },
  uColourNew: { value: [0, 0, 0, 1] as number[], type: 'vec4<f32>' as const },
  uPage: { value: [1, 1, 1, 1] as number[], type: 'vec4<f32>' as const },
  uSwipe: { value: 0, type: 'f32' as const },
  uDivider: { value: 0.5, type: 'f32' as const },
};

type CompareUniformStruct = typeof COMPARE_UNIFORM_STRUCT;

function paletteVector(colour: Rgb01): number[] {
  return [colour.r, colour.g, colour.b, 1];
}

/**
 * A custom PixiJS v8 `Filter` that composites two baked layer textures.
 * The uniform values are mutated through a `UniformGroup`, which Pixi
 * uploads on every frame it is drawn, so pan/zoom re-bakes and slider
 * changes need no filter rebuild — the object stays alive for the lifetime
 * of one pair of render textures (recreated only when the canvas resizes).
 *
 * `pageColour` is the sheet's paper token (`--sheet`, warm white) — the
 * overlay classification paints the sheet background with it so composite
 * modes match the raw-sheet modes exactly.
 */
export class LayerCompareFilter {
  /** The Pixi filter; attach it to the full-canvas composite sprite. */
  readonly filter: Filter;

  private readonly compareUniforms: UniformGroup<CompareUniformStruct>;

  constructor(oldTexture: TextureSource, newTexture: TextureSource, pageColour: Rgb01) {
    this.compareUniforms = new UniformGroup<CompareUniformStruct>(COMPARE_UNIFORM_STRUCT);
    this.compareUniforms.uniforms.uPage = paletteVector(pageColour);
    this.filter = new Filter({
      glProgram: new GlProgram({
        vertex: LAYER_COMPARE_VERTEX,
        fragment: LAYER_COMPARE_FRAGMENT,
        name: 'cqdc-layer-compare',
      }),
      // Named texture resources reach the GL program's samplers by name
      // (Pixi binds each `TextureSource` in `resources` to the uniform of
      // the same name); the group carries the scalar uniforms.
      resources: {
        compareUniforms: this.compareUniforms,
        uTexA: oldTexture,
        uTexB: newTexture,
      },
      resolution: 'inherit',
    });
    this.setSettings({
      swipe: false,
      dividerFrac: 0.5,
      oldOpacity: 1,
      palette: DEFAULT_DIFF_PALETTE,
    });
  }

  setSettings(settings: LayerCompareSettings): void {
    const uniforms = this.compareUniforms.uniforms;
    uniforms.uOldOpacity = settings.oldOpacity;
    uniforms.uSwipe = settings.swipe ? 1 : 0;
    uniforms.uDivider = settings.dividerFrac;
    uniforms.uColourOld = paletteVector(settings.palette.oldInk);
    uniforms.uColourNew = paletteVector(settings.palette.newInk);
    this.compareUniforms.update();
  }

  destroy(): void {
    this.filter.destroy();
  }
}

// ── Canvas 2D reference math (mirror of the GLSL) ──────────────────────

/**
 * One overlay pixel of the Canvas 2D fallback, mirroring the fragment
 * shader above so both backends show the same semantics. Inputs are
 * straight-alpha RGBA on 0..255 (as `ImageData` stores them — the two
 * low-resolution bakes). Returns straight RGBA on 0..255; `alpha` 0 means
 * "no sheet here", and the caller leaves the pixel transparent.
 * `pageColour` is the sheet token colour the pixel starts from.
 */
export function compositeOverlayPixel(
  a: [number, number, number, number],
  b: [number, number, number, number],
  oldOpacity: number,
  palette: DiffPalette,
  pageColour: Rgb01,
): [number, number, number, number] {
  const presenceA = a[3] / 255;
  const presenceB = b[3] / 255;
  const lumaA = (a[0] * 0.299 + a[1] * 0.587 + a[2] * 0.114) / 255;
  const lumaB = (b[0] * 0.299 + b[1] * 0.587 + b[2] * 0.114) / 255;

  const inkA = Math.min(1, Math.max(0, presenceA * (1 - lumaA))) * oldOpacity;
  const inkB = Math.min(1, Math.max(0, presenceB * (1 - lumaB)));
  const both = Math.min(inkA, inkB);
  const onlyA = inkA - both;
  const onlyB = inkB - both;

  const alpha = Math.min(1, presenceA + presenceB);
  const mix = (from: number, to: number, t: number): number => from + (to - from) * t;
  const channel = (page: number, old: number, next: number): number => {
    let value = page; // the warm sheet background
    value = mix(value, old, onlyA);
    value = mix(value, next, onlyB);
    value = mix(value, 0, COINCIDENT_INK_ALPHA * both);
    return value;
  };

  return [
    channel(pageColour.r, palette.oldInk.r, palette.newInk.r) * 255,
    channel(pageColour.g, palette.oldInk.g, palette.newInk.g) * 255,
    channel(pageColour.b, palette.oldInk.b, palette.newInk.b) * 255,
    alpha * 255,
  ];
}
