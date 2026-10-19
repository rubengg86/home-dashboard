export const fmtW = (w: number | null | undefined): string =>
  w == null ? '–' : w >= 1000 ? `${(w / 1000).toFixed(2)} kW` : `${Math.round(w)} W`;

export const fmtKWh = (v: number | null | undefined, fine = false): string =>
  v == null ? '–' : `${v.toFixed(fine && v < 1 ? 3 : v < 10 ? 2 : 1)} kWh`;

export const fmtMoney = (v: number | null | undefined, cur: string): string =>
  v == null ? '–' : `${cur}${v.toFixed(2)}`;

export const hhmm = (iso: string | number | Date): string =>
  new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

export const minutesUntil = (iso: string | null): number | null =>
  iso ? Math.max(0, Math.ceil((new Date(iso).getTime() - Date.now()) / 60_000)) : null;

const EVENT_LABEL: Record<string, string> = {
  singleClick: 'Single press', doubleClick: 'Double press', rotation: 'Rotated',
  started: 'Cycle started', finished: 'Cycle finished', heating: 'Heating',
  hot_water_ready: 'Hot water ready', safety_off: 'Safety off', on: 'On', off: 'Off',
};
export const eventLabel = (e: string): string => EVENT_LABEL[e] ?? e;

export function timeAgo(iso: string): string {
  const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString([], { day: 'numeric', month: 'short' });
}
