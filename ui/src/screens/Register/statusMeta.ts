import type { StatusTone } from '../../components/StatusPill';
import type { RegisterStatus } from '../../api/types';

/**
 * How each status reads and looks.
 *
 * The status palette comes from the tokens' status colours, never from brand
 * red. Brand red belongs to the chrome; using it for data would make a
 * "removed" row look like a button.
 */
export const STATUS_LABEL: Record<RegisterStatus, string> = {
  revised: 'Revised',
  unchanged: 'Unchanged',
  same_rev_different_file: 'Same revision, different file',
  new: 'New',
  not_reissued: 'Not reissued',
  removed: 'Removed',
  status_change: 'Status change',
  superseded_in_folder: 'Superseded in folder',
  duplicate_file: 'Duplicate file',
  unidentified: 'Unidentified',
  unreadable: 'Could not be read',
  in_list_not_in_folder: 'On the list, not in the folders',
  in_folder_not_in_list: 'In the folder, not on the list',
};

export const STATUS_TONE: Record<RegisterStatus, StatusTone> = {
  revised: 'info',
  unchanged: 'neutral',
  same_rev_different_file: 'warn',
  new: 'ok',
  not_reissued: 'neutral',
  removed: 'danger',
  status_change: 'info',
  superseded_in_folder: 'neutral',
  duplicate_file: 'neutral',
  unidentified: 'warn',
  unreadable: 'danger',
  in_list_not_in_folder: 'warn',
  in_folder_not_in_list: 'warn',
};

/** The order counts appear in the summary bar: the interesting things first. */
export const SUMMARY_ORDER: RegisterStatus[] = [
  'revised',
  'status_change',
  'new',
  'same_rev_different_file',
  'removed',
  'not_reissued',
  'unchanged',
  'in_list_not_in_folder',
  'in_folder_not_in_list',
  'unidentified',
  'unreadable',
];

/** How each `source_of_number` reads, for the detail panel. */
export const SOURCE_LABEL: Record<string, string> = {
  titleblock: 'Title block',
  sheet_text: 'Sheet text',
  filename: 'File name',
  drawing_list: 'Drawing list',
  user: 'Entered by you',
  ai: 'Read by AI',
  none: 'Not found',
};
