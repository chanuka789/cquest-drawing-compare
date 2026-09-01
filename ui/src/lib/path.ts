/**
 * Windows path helpers for display.
 *
 * Real drawing paths run past 260 characters, so a panel can only ever show
 * part of one. The start and the end carry the meaning — the server and the
 * issue folder — so the middle is what gets dropped.
 */

const SEPARATORS = /[\\/]+/;

/** The last segment of a path: the folder or file name. */
export function baseName(path: string): string {
  const parts = path.split(SEPARATORS).filter((part) => part.length > 0);
  return parts.length > 0 ? (parts[parts.length - 1] as string) : path;
}

/** Everything before the last segment. Empty when there is no parent. */
export function parentPath(path: string): string {
  const trimmed = path.replace(/[\\/]+$/, '');
  const index = Math.max(trimmed.lastIndexOf('\\'), trimmed.lastIndexOf('/'));
  return index > 0 ? trimmed.slice(0, index) : '';
}

/**
 * Shorten a path by removing the middle, keeping both ends readable.
 * Always pair this with the full path in a `title` attribute.
 */
export function truncateMiddle(text: string, maxLength = 52): string {
  if (text.length <= maxLength) return text;

  const ellipsis = '…';
  const keep = maxLength - ellipsis.length;
  const head = Math.ceil(keep * 0.55);
  const tail = keep - head;

  return `${text.slice(0, head)}${ellipsis}${text.slice(text.length - tail)}`;
}

/** True when two chosen folders are the same place. */
export function isSameFolder(a: string | null, b: string | null): boolean {
  if (!a || !b) return false;
  const normalise = (value: string): string =>
    value.replace(/[\\/]+$/, '').replace(/\//g, '\\').toLowerCase();
  return normalise(a) === normalise(b);
}
