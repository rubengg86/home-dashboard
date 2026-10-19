import { Component, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { IonContent, IonHeader, IonLabel, IonRefresher, IonRefresherContent, IonSegment, IonSegmentButton, IonTitle, IonToolbar } from '@ionic/angular';
import { fmtKWh, fmtMoney, fmtW } from '../core/format';
import { LiveService } from '../core/live.service';
import { EnergyRange } from '../core/models';
import { ChartSeries, EnergyChartComponent, bucketLabel } from './energy-chart.component';

const SLOTS = ['var(--s1)', 'var(--s2)', 'var(--s3)', 'var(--s4)'];
const AVG = { '24h': 'Avg / hour', '7d': 'Avg / day', '30d': 'Avg / day', '12m': 'Avg / month' } as const;

@Component({
  selector: 'app-energy',
  templateUrl: 'energy.page.html',
  styleUrls: ['energy.page.scss'],
  imports: [FormsModule, IonHeader, IonToolbar, IonTitle, IonContent, IonRefresher, IonRefresherContent,
            IonSegment, IonSegmentButton, IonLabel, EnergyChartComponent],
})
export class EnergyPage {
  live = inject(LiveService);
  readonly fmtW = fmtW;
  range = signal<EnergyRange>('30d');
  unit = signal<'kwh' | 'cost'>('kwh');
  showTable = signal(false);

  readonly data = this.live.energy;
  readonly priced = computed(() => (this.data()?.price ?? 0) > 0);
  readonly cost = computed(() => this.unit() === 'cost' && this.priced());
  readonly scale = computed(() => (this.cost() ? this.data()!.price : 1));
  readonly fine = computed(() => this.range() === '24h');
  /** Formats a (possibly cost-scaled) value for the current unit. */
  readonly fmt = computed(() => {
    const cur = this.data()?.currency ?? '€', cost = this.cost(), fine = this.fine();
    return (v: number) => (cost ? fmtMoney(v, cur) : fmtKWh(v, fine));
  });

  /** Colour follows the device: slot by position in the API's (stable) series order. */
  readonly series = computed<ChartSeries[]>(() => {
    const d = this.data();
    if (!d) return [];
    const r = d.ranges[this.range()];
    return d.series.map((s, i) => ({ id: s.id, name: s.name, color: SLOTS[i % SLOTS.length], values: r.values[s.id] ?? [] }));
  });
  readonly buckets = computed(() => this.data()?.ranges[this.range()].buckets ?? []);

  readonly stats = computed(() => {
    const d = this.data();
    if (!d) return null;
    const n = this.buckets().length;
    let total = 0, withData = 0;
    for (let i = 0; i < n; i++) {
      const vals = this.series().map((s) => s.values[i]).filter((v): v is number => v != null);
      if (vals.length) { withData++; total += vals.reduce((a, b) => a + b, 0); }
    }
    const avg = withData ? total / withData : null;
    return {
      kwh: fmtKWh(total, this.fine()),
      cost: d.price > 0 ? fmtMoney(total * d.price, d.currency) : '–',
      avgLabel: AVG[this.range()],
      avg: avg == null ? '–' : this.cost() ? fmtMoney(avg * d.price, d.currency) : fmtKWh(avg, this.fine()),
      total,
    };
  });

  /** Per-device totals for the range, biggest first, with their share. */
  readonly breakdown = computed(() => {
    const d = this.data(), st = this.stats();
    if (!d || !st) return [];
    return this.series().map((s) => {
      const kwh = s.values.reduce<number>((a, v) => a + (v ?? 0), 0);
      return { ...s, kwh, value: this.fmt()(kwh * this.scale()), share: st.total > 0 ? (kwh / st.total) * 100 : 0,
               kind: d.series.find((x) => x.id === s.id)?.kind, since: d.series.find((x) => x.id === s.id)?.since };
    }).sort((a, b) => b.kwh - a.kwh);
  });

  readonly table = computed(() => {
    const rows = this.buckets().map((iso, i) => {
      const vals = this.series().map((s) => s.values[i] ?? null);
      const known = vals.filter((v): v is number => v != null);
      const kwh = known.length ? known.reduce((a, b) => a + b, 0) : null;
      return { label: bucketLabel(iso, this.range(), true), vals: vals.map((v) => (v == null ? '–' : fmtKWh(v, this.fine()))),
               kwh: kwh == null ? '–' : fmtKWh(kwh, this.fine()),
               cost: kwh != null && this.priced() ? fmtMoney(kwh * this.data()!.price, this.data()!.currency) : '' };
    });
    return rows.reverse();
  });

  async doRefresh(ev: CustomEvent): Promise<void> {
    await Promise.all([this.live.loadEnergy(), this.live.refresh()]);
    (ev.target as HTMLIonRefresherElement).complete();
  }
}
