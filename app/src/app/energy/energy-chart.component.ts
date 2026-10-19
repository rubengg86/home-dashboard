import { Component, ElementRef, OnDestroy, OnInit, computed, effect, inject, input, signal } from '@angular/core';
import { EnergyRange } from '../core/models';

export interface ChartSeries { id: string; name: string; color: string; values: (number | null)[] }
export interface ChartRow { iso: string; vals: (number | null)[]; total: number | null }

/** Smallest axis top >= v made of 3-5 round steps (1, 2, 2.5 or 5 x 10^k). */
function niceScale(v: number): { max: number; step: number } {
  if (!(v > 0)) return { max: 1, step: 0.25 };
  let best = { max: Infinity, step: 1 };
  const k0 = Math.floor(Math.log10(v));
  for (let k = k0 - 2; k <= k0 + 1; k++)
    for (const m of [1, 2, 2.5, 5]) for (const ticks of [3, 4, 5]) {
      const step = m * 10 ** k, max = step * ticks;
      if (max >= v && max < best.max) best = { max, step };
    }
  return best;
}
const decimals = (step: number) => { let d = 0; while (d < 6 && Math.abs(step * 10 ** d - Math.round(step * 10 ** d)) > 1e-9) d++; return d; };

export function bucketLabel(iso: string, range: EnergyRange, long: boolean): string {
  const d = new Date(iso);
  if (range === '24h') {
    const h = d.getHours(), p = (n: number) => String(n).padStart(2, '0');
    return long ? `${d.toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' })} · ${p(h)}:00–${p((h + 1) % 24)}:00` : p(h);
  }
  if (range === '12m') return d.toLocaleDateString([], long ? { month: 'long', year: 'numeric' } : { month: 'short' });
  if (range === '7d' && !long) return d.toLocaleDateString([], { weekday: 'short' });
  return d.toLocaleDateString([], long ? { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' } : { day: 'numeric', month: 'short' });
}

/**
 * Stacked bars, one segment per device. Touch: tap or drag across the bars to select one; the readout above
 * the chart shows that bar's values (a tooltip under a finger would be hidden). Desktop: hover works too.
 */
@Component({
  selector: 'app-energy-chart',
  template: `
    <div class="readout" aria-live="polite">
      @if (sel(); as r) {
        <div class="when">{{ label(r.iso, true) }}{{ selIndex() === rows().length - 1 ? ' · so far' : '' }}</div>
        @for (s of series(); track s.id; let i = $index) {
          <div class="line"><i [style.background]="s.color"></i><span class="n">{{ s.name }}</span>
            <b>{{ r.vals[i] == null ? 'no data' : fmt()(r.vals[i]!) }}</b></div>
        }
        @if (series().length > 1) {
          <div class="line tot"><i></i><span class="n">Total</span><b>{{ r.total == null ? '–' : fmt()(r.total) }}</b></div>
        }
      }
    </div>
    <svg [attr.viewBox]="'0 0 ' + w() + ' ' + H" [attr.height]="H" role="img" tabindex="0"
         [attr.aria-label]="'Electricity per ' + per() + '. Tap a bar or use the arrow keys.'"
         (pointerdown)="pick($event)" (pointermove)="pick($event)" (keydown)="key($event)">
      @for (t of ticks(); track t.v) {
        <line [attr.x1]="padL" [attr.x2]="w() - padR" [attr.y1]="t.y" [attr.y2]="t.y" [class.base]="t.v === 0" class="grid-l" />
        <text [attr.x]="padL - 6" [attr.y]="t.y + 3.5" text-anchor="end">{{ t.label }}</text>
      }
      <text [attr.x]="padL - 6" y="10" text-anchor="end">{{ unitLabel() }}</text>
      <rect class="hl" [attr.x]="padL + selIndex() * slot()" [attr.y]="padT" [attr.width]="slot()" [attr.height]="ih" rx="4" />
      @for (b of bars(); track b.i) {
        <g [class.dim]="b.i !== selIndex()">
          @for (s of b.segs; track s.si) { <path [attr.d]="s.d" [attr.fill]="s.color" /> }
        </g>
        @if (b.label) { <text [attr.x]="b.cx" [attr.y]="H - 6" [attr.text-anchor]="b.anchor">{{ b.label }}</text> }
      }
    </svg>
  `,
  styles: [`
    :host { display: block; }
    .readout { min-height: 92px; background: var(--app-card-2); border-radius: 12px; padding: 10px 12px; margin-bottom: 10px; font-size: 13.5px; }
    .when { color: var(--app-muted); margin-bottom: 4px; }
    .line { display: flex; align-items: center; gap: 8px; margin-top: 3px; }
    .line i { width: 12px; height: 3px; border-radius: 2px; flex: none; }
    .line.tot { border-top: 1px solid var(--app-line); margin-top: 6px; padding-top: 6px; }
    .n { flex: 1; color: var(--app-muted); min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    b { font-variant-numeric: tabular-nums; }
    svg { display: block; width: 100%; touch-action: pan-y; outline: none; user-select: none; }
    svg:focus-visible { box-shadow: 0 0 0 2px var(--ion-color-primary); border-radius: 6px; }
    text { fill: var(--app-muted); font-size: 11px; font-variant-numeric: tabular-nums; }
    .grid-l { stroke: var(--app-line); stroke-width: 1; }
    .grid-l.base { stroke: var(--app-muted); stroke-opacity: .5; }
    .hl { fill: var(--ion-text-color, #000); opacity: .06; }
    g.dim path { opacity: .5; }
  `],
})
export class EnergyChartComponent implements OnInit, OnDestroy {
  private el = inject(ElementRef<HTMLElement>);
  series = input.required<ChartSeries[]>();
  buckets = input.required<string[]>();
  range = input.required<EnergyRange>();
  /** Formats a value (kWh or money) for the readout. */
  fmt = input.required<(v: number) => string>();
  unitLabel = input('kWh');
  /** Multiplier applied to every value (price for cost mode). */
  scale = input(1);

  readonly H = 200; readonly padL = 40; readonly padR = 2; readonly padT = 20; readonly padB = 22;
  readonly ih = this.H - this.padT - this.padB;
  w = signal(340);
  selIndex = signal(0);
  private ro?: ResizeObserver;

  readonly per = computed(() => ({ '24h': 'hour', '7d': 'day', '30d': 'day', '12m': 'month' })[this.range()]);
  readonly rows = computed<ChartRow[]>(() => this.buckets().map((iso, i) => {
    const vals = this.series().map((s) => s.values[i] == null ? null : s.values[i]! * this.scale());
    const known = vals.filter((v): v is number => v != null);
    return { iso, vals, total: known.length ? known.reduce((a, b) => a + b, 0) : null };
  }));
  readonly sel = computed(() => this.rows()[this.selIndex()] ?? null);
  readonly slot = computed(() => (this.w() - this.padL - this.padR) / Math.max(1, this.rows().length));
  private readonly axis = computed(() => niceScale(Math.max(0, ...this.rows().map((r) => r.total ?? 0))));
  private y = (v: number) => this.padT + this.ih - (v / this.axis().max) * this.ih;

  readonly ticks = computed(() => {
    const { max, step } = this.axis(), d = decimals(step), out = [];
    for (let i = 0; i * step <= max + 1e-9; i++) out.push({ v: i * step, y: Math.round(this.y(i * step)) + 0.5, label: (i * step).toFixed(d) });
    return out;
  });

  readonly bars = computed(() => {
    const n = this.rows().length, slot = this.slot();
    const gap = Math.max(2, Math.min(slot * 0.28, 10)), bw = Math.max(1, slot - gap);
    const every = this.range() === '24h' ? 6 : this.range() === '30d' ? 7 : this.range() === '12m' && this.w() < 360 ? 2 : 1;
    const colors = this.series().map((s) => s.color);
    return this.rows().map((r, i) => {
      const x = this.padL + i * slot + gap / 2;
      // stacked bottom-up, 2px surface gap between segments, 4px rounded top on the top segment only
      const parts = r.vals.map((v, si) => ({ v: v ?? 0, si })).filter((p) => p.v > 0);
      let acc = 0;
      const segs = parts.map((p, k) => {
        const y0 = this.y(acc), y1 = this.y(acc + p.v); acc += p.v;
        const top = k === parts.length - 1, h = Math.max(0, y0 - y1 - (top ? 0 : 2)), yt = y0 - h;
        const rr = top ? Math.min(4, bw / 2, h) : 0;
        return { si: p.si, color: colors[p.si], d: h <= 0 ? '' :
          `M${x},${y0} V${yt + rr} Q${x},${yt} ${x + rr},${yt} H${x + bw - rr} Q${x + bw},${yt} ${x + bw},${yt + rr} V${y0} Z` };
      }).filter((s) => s.d);
      // edge labels anchor inwards so they aren't clipped
      const cx = x + bw / 2, anchor = cx > this.w() - 24 ? 'end' : cx < this.padL + 16 ? 'start' : 'middle';
      return { i, segs, cx: anchor === 'end' ? x + bw : anchor === 'start' ? x : cx, anchor,
               label: i % every === (n - 1) % every ? bucketLabel(r.iso, this.range(), false) : '' };
    });
  });

  constructor() {
    // new range/data: select the latest bar
    effect(() => { this.selIndex.set(Math.max(0, this.buckets().length - 1)); });
  }

  ngOnInit(): void {
    this.ro = new ResizeObserver(([e]) => e.contentRect.width && this.w.set(Math.max(260, e.contentRect.width)));
    this.ro.observe(this.el.nativeElement);
  }

  ngOnDestroy(): void { this.ro?.disconnect(); }

  label(iso: string, long: boolean): string { return bucketLabel(iso, this.range(), long); }

  pick(ev: PointerEvent): void {
    if (ev.type === 'pointermove' && ev.pointerType !== 'mouse' && !ev.buttons) return;
    const svg = ev.currentTarget as SVGSVGElement, box = svg.getBoundingClientRect();
    const x = (ev.clientX - box.left) * (this.w() / box.width);
    const i = Math.floor((x - this.padL) / this.slot());
    if (i >= 0 && i < this.rows().length) this.selIndex.set(i);
  }

  key(ev: KeyboardEvent): void {
    if (ev.key !== 'ArrowLeft' && ev.key !== 'ArrowRight') return;
    ev.preventDefault();
    this.selIndex.update((i) => Math.max(0, Math.min(this.rows().length - 1, i + (ev.key === 'ArrowLeft' ? -1 : 1))));
  }
}
