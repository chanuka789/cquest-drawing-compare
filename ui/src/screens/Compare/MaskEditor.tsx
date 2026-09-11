/**
 * Screen 6 — the mask editor (Phase 5, Task 5.4).
 *
 * What is never compared, shown on the sheet it was found on, with the
 * reason it was excluded written next to it.
 *
 * The default flow is ten seconds long: the engine has already clustered the
 * project into two or three templates and detected the zones on one sheet
 * per cluster, so the user looks, adjusts if anything is wrong, and presses
 * "Apply to all N sheets". Nothing here ever asks for per-sheet work.
 *
 * Two things the screen is careful about:
 *
 *  - **Protected regions are shown as kept, not as masked.** North arrow,
 *    scale bar, key plan and general notes are listed separately so it is
 *    obvious the application decided to keep comparing them.
 *  - **A low-confidence detection is proposed, not applied.** It is drawn
 *    dashed and says so, because wrongly masking a region hides a real
 *    change and nobody ever finds out.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { thumbnailUrl } from '../../api/client';
import type { FracRect, MaskZone, ProtectedRegion } from '../../api/types';
import {
  activeTemplate,
  affectedSheets,
  isNegligible,
  normaliseRect,
  useMaskStore,
} from '../../store/maskStore';
import { useAppStore } from '../../store/appStore';

import './MaskEditor.css';

/** Plain-English names for the zone types, in sentence case. */
const ZONE_NAMES: Record<string, string> = {
  titleblock: 'Title block',
  revision_table: 'Revision history',
  logo: 'Logo',
  watermark: 'Watermark',
  stamp: 'Stamp',
  annotation: 'Annotation',
  frame: 'Sheet frame',
  user: 'Your zone',
};

const PROTECTED_NAMES: Record<string, string> = {
  north_arrow: 'North arrow',
  scale_bar: 'Scale bar',
  key_plan: 'Key plan',
  general_notes: 'General notes',
  user_protected: 'Always compared',
};

function percent(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

function styleFor(rect: FracRect): React.CSSProperties {
  return {
    left: percent(rect.x0),
    top: percent(rect.y0),
    width: percent(rect.x1 - rect.x0),
    height: percent(rect.y1 - rect.y0),
  };
}

export function MaskEditor() {
  const goToAlignment = useAppStore((state) => state.goToAlignment);
  const store = useMaskStore();
  const template = activeTemplate(store);
  const sheets = affectedSheets(store);

  const canvasRef = useRef<HTMLDivElement | null>(null);
  const [draft, setDraft] = useState<FracRect | null>(null);
  const dragStart = useRef<{ x: number; y: number } | null>(null);

  const load = useMaskStore((state) => state.load);
  const resetStore = useMaskStore((state) => state.reset);

  useEffect(() => {
    // Detection is the expensive part and it is per template, not per
    // render: load once on mount and drop the working copies on the way out
    // so a second visit starts from the engine's detection, not from a
    // half-finished edit.
    void load();
    return resetStore;
  }, [load, resetStore]);

  const pointToFraction = useCallback((event: React.PointerEvent): { x: number; y: number } => {
    const element = canvasRef.current;
    if (element === null) return { x: 0, y: 0 };
    const bounds = element.getBoundingClientRect();
    return {
      x: (event.clientX - bounds.left) / bounds.width,
      y: (event.clientY - bounds.top) / bounds.height,
    };
  }, []);

  const drawing = store.tool === 'rectangle' || store.tool === 'protect';

  const onPointerDown = (event: React.PointerEvent) => {
    if (!drawing) return;
    const point = pointToFraction(event);
    dragStart.current = point;
    setDraft({ x0: point.x, y0: point.y, x1: point.x, y1: point.y });
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  const onPointerMove = (event: React.PointerEvent) => {
    if (dragStart.current === null) return;
    const point = pointToFraction(event);
    setDraft(
      normaliseRect({
        x0: dragStart.current.x,
        y0: dragStart.current.y,
        x1: point.x,
        y1: point.y,
      }),
    );
  };

  const onPointerUp = () => {
    if (draft !== null && !isNegligible(draft)) {
      if (store.tool === 'protect') store.addProtected(draft);
      else store.addZone(draft);
    }
    dragStart.current = null;
    setDraft(null);
  };

  const headline = useMemo(() => {
    if (store.loading) return 'Reading the sheets…';
    if (store.templates.length === 0) return 'No sheets to build a template from.';
    const count = store.templates.length;
    const word = count === 1 ? 'template' : 'templates';
    return `${count} sheet ${word} across ${store.templates.reduce(
      (total, entry) => total + entry.sheet_count,
      0,
    )} sheets. Check what is excluded, then apply it to the whole set.`;
  }, [store.loading, store.templates]);

  return (
    <section className="mask">
      <header className="mask__head">
        <div className="mask__title-row">
          <button type="button" className="mask__back" onClick={goToAlignment}>
            Back to alignment
          </button>
          <h1 className="mask__title">What is not compared</h1>
        </div>
        <p className="mask__lede">{headline}</p>
        {store.errorMessage !== null && (
          <p className="mask__error" role="alert">
            {store.errorMessage}
          </p>
        )}
      </header>

      <div className="mask__body">
        <aside className="mask__side">
          <h2 className="mask__side-title">Templates</h2>
          <ul className="mask__templates">
            {store.templates.map((entry) => (
              <li key={entry.template_id}>
                <button
                  type="button"
                  className={
                    entry.template_id === store.activeId
                      ? 'mask__template mask__template--on'
                      : 'mask__template'
                  }
                  onClick={() => store.selectTemplate(entry.template_id)}
                >
                  <span className="mask__template-name">
                    {entry.sheet_count} sheet{entry.sheet_count === 1 ? '' : 's'}
                  </span>
                  <span className="mask__template-state">
                    {entry.confirmed ? 'Confirmed' : 'Not confirmed'}
                  </span>
                </button>
              </li>
            ))}
          </ul>

          <h2 className="mask__side-title">Excluded</h2>
          <ul className="mask__zones">
            {store.zones.map((zone, index) => (
              <ZoneRow
                key={`${zone.type}-${index}`}
                zone={zone}
                index={index}
                selected={store.selected === index}
                onSelect={() => store.select(index)}
                onToggle={() => store.toggleZone(index)}
                onRemove={() => store.removeZone(index)}
              />
            ))}
            {store.zones.length === 0 && (
              <li className="mask__empty">
                Nothing is excluded on this template. Draw a zone if the revision panel shows
                up as a change.
              </li>
            )}
          </ul>

          <h2 className="mask__side-title">Kept, deliberately</h2>
          <ul className="mask__protected">
            {store.protectedRegions.map((region, index) => (
              <ProtectedRow
                key={`${region.type}-${index}`}
                region={region}
                onRemove={() => store.removeProtected(index)}
              />
            ))}
            {store.protectedRegions.length === 0 && (
              <li className="mask__empty">
                Nothing was singled out to keep. A change anywhere outside the excluded zones
                is still reported.
              </li>
            )}
          </ul>
        </aside>

        <div className="mask__canvas-wrap">
          <div className="mask__tools">
            <ToolButton
              label="Select"
              active={store.tool === 'select'}
              onClick={() => store.setTool('select')}
            />
            <ToolButton
              label="Draw exclusion"
              active={store.tool === 'rectangle'}
              onClick={() => store.setTool('rectangle')}
            />
            <ToolButton
              label="Mark as kept"
              active={store.tool === 'protect'}
              onClick={() => store.setTool('protect')}
            />
            <label className="mask__preview-toggle">
              <input type="checkbox" checked={store.preview} onChange={store.togglePreview} />
              Show what will be compared
            </label>
          </div>

          <div
            ref={canvasRef}
            className={store.preview ? 'mask__canvas mask__canvas--preview' : 'mask__canvas'}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            data-drawing={drawing ? 'yes' : 'no'}
          >
            {template !== null && (
              <img
                className="mask__sheet"
                src={thumbnailUrl(template.representative_sheet_id)}
                alt="The representative sheet for this template"
                draggable={false}
              />
            )}

            {store.zones.map((zone, index) =>
              zone.enabled ? (
                <div
                  key={`zone-${index}`}
                  className={[
                    'mask__zone',
                    `mask__zone--${zone.type}`,
                    zone.needs_confirmation ? 'mask__zone--proposed' : '',
                    store.selected === index ? 'mask__zone--selected' : '',
                    zone.ink_only ? 'mask__zone--ink' : '',
                  ]
                    .filter(Boolean)
                    .join(' ')}
                  style={styleFor(zone.rect)}
                  onClick={() => store.select(index)}
                  title={zone.evidence.join(' ')}
                >
                  <span className="mask__zone-tag">{ZONE_NAMES[zone.type] ?? zone.type}</span>
                </div>
              ) : null,
            )}

            {store.protectedRegions.map((region, index) => (
              <div
                key={`protected-${index}`}
                className="mask__kept"
                style={styleFor(region.rect)}
                title={region.evidence.join(' ')}
              >
                <span className="mask__zone-tag">
                  {PROTECTED_NAMES[region.type] ?? region.label}
                </span>
              </div>
            ))}

            {draft !== null && <div className="mask__draft" style={styleFor(draft)} />}
          </div>

          <footer className="mask__actions">
            {store.appliedTo !== null && (
              <span className="mask__applied" role="status">
                Applied to {store.appliedTo} sheet{store.appliedTo === 1 ? '' : 's'}.
              </span>
            )}
            <button
              type="button"
              className="mask__apply"
              onClick={() => void store.apply()}
              disabled={store.saving || store.activeId === null}
            >
              {store.saving ? 'Applying…' : `Apply to all ${sheets} sheets using this template`}
            </button>
          </footer>
        </div>
      </div>
    </section>
  );
}

function ToolButton({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      className={active ? 'mask__tool mask__tool--on' : 'mask__tool'}
      onClick={onClick}
      aria-pressed={active}
    >
      {label}
    </button>
  );
}

function ZoneRow({
  zone,
  index,
  selected,
  onSelect,
  onToggle,
  onRemove,
}: {
  zone: MaskZone;
  index: number;
  selected: boolean;
  onSelect: () => void;
  onToggle: () => void;
  onRemove: () => void;
}) {
  return (
    <li className={selected ? 'mask__zone-row mask__zone-row--on' : 'mask__zone-row'}>
      <button type="button" className="mask__zone-main" onClick={onSelect}>
        <span className={`mask__swatch mask__swatch--${zone.type}`} aria-hidden="true" />
        <span className="mask__zone-label">{zone.label || ZONE_NAMES[zone.type] || zone.type}</span>
        {zone.needs_confirmation && <span className="mask__proposed">Proposed</span>}
        {zone.ink_only && <span className="mask__ink">Its ink only</span>}
      </button>
      <p className="mask__evidence">{zone.evidence.join(' ')}</p>
      <div className="mask__zone-buttons">
        <button type="button" onClick={onToggle}>
          {zone.enabled ? 'Switch off' : 'Switch on'}
        </button>
        <button type="button" onClick={onRemove} aria-label={`Delete zone ${index + 1}`}>
          Delete
        </button>
      </div>
    </li>
  );
}

function ProtectedRow({
  region,
  onRemove,
}: {
  region: ProtectedRegion;
  onRemove: () => void;
}) {
  return (
    <li className="mask__kept-row">
      <span className="mask__kept-label">
        {PROTECTED_NAMES[region.type] ?? region.label}
      </span>
      <p className="mask__evidence">{region.evidence.join(' ')}</p>
      <button type="button" onClick={onRemove}>
        Stop keeping
      </button>
    </li>
  );
}
