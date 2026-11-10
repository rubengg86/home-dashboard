import { HttpClient } from '@angular/common/http';
import { Component, inject, signal } from '@angular/core';
import { DecimalPipe } from '@angular/common';
import {
  IonButton, IonButtons, IonContent, IonHeader, IonIcon, IonRefresher, IonRefresherContent,
  IonTitle, IonToggle, IonToolbar, ViewWillEnter,
} from '@ionic/angular';
import { addIcons } from 'ionicons';
import { addOutline, trashOutline } from 'ionicons/icons';
import { firstValueFrom } from 'rxjs';
import { errorText, LiveService } from '../core/live.service';
import { HeatingScheduleDoc, ScheduleBlock, ScheduleProgram } from '../core/models';
import { UiService } from '../shared/ui.service';

const DAY = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

@Component({
  selector: 'app-schedule',
  templateUrl: 'schedule.page.html',
  styleUrls: ['schedule.page.scss'],
  imports: [DecimalPipe, IonHeader, IonToolbar, IonTitle, IonButtons, IonButton, IonIcon, IonContent,
            IonRefresher, IonRefresherContent, IonToggle],
})
export class SchedulePage implements ViewWillEnter {
  live = inject(LiveService);
  private http = inject(HttpClient);
  private ui = inject(UiService);

  readonly doc = signal<HeatingScheduleDoc | null>(null);
  readonly dirty = signal(false);
  readonly busy = signal(false);
  readonly error = signal<string | null>(null);

  constructor() {
    addIcons({ addOutline, trashOutline });
  }

  ionViewWillEnter(): void {
    if (!this.dirty()) this.load();  // don't wipe unsaved edits when tabbing back
  }

  async load(): Promise<void> {
    try {
      this.doc.set(await firstValueFrom(this.http.get<HeatingScheduleDoc>('/api/v1/heating/schedule')));
      this.dirty.set(false);
      this.error.set(null);
    } catch (e) {
      this.error.set(errorText(e));
    }
  }

  async doRefresh(ev: CustomEvent): Promise<void> {
    if (!this.dirty()) await this.load();
    (ev.target as HTMLIonRefresherElement).complete();
  }

  async save(): Promise<void> {
    const doc = this.doc();
    if (!doc) return;
    this.busy.set(true);
    try {
      await this.ui.run(async () => {
        this.doc.set(await firstValueFrom(this.http.put<HeatingScheduleDoc>('/api/v1/heating/schedule', doc)));
        this.dirty.set(false);
        this.ui.toast('Schedule saved');
      });
    } finally {
      this.busy.set(false);
    }
  }

  // ------------------------------------------------------------ edits (all mark the doc dirty)
  private touch(): void {
    this.doc.update((d) => d ? { ...d } : d);
    this.dirty.set(true);
  }

  setEnabled(on: boolean): void {
    const d = this.doc();
    if (d && d.enabled !== on) { d.enabled = on; this.touch(); }
  }

  nudge(b: ScheduleBlock, id: string, dir: 1 | -1): void {
    const cur = b.targets[id];
    b.targets[id] = Math.min(30, Math.max(5, (cur ?? 16) + (cur == null ? 0 : dir * 0.5)));
    this.touch();
  }

  clearTarget(b: ScheduleBlock, id: string): void {
    delete b.targets[id];
    this.touch();
  }

  setTime(p: ScheduleProgram, b: ScheduleBlock, time: string): void {
    if (!/^\d{2}:\d{2}$/.test(time) || p.blocks.some((x) => x !== b && x.time === time)) return this.touch();
    b.time = time;
    p.blocks.sort((a, z) => a.time.localeCompare(z.time));
    this.touch();
  }

  addBlock(p: ScheduleProgram): void {
    const last = p.blocks[p.blocks.length - 1];
    let [h] = (last?.time ?? '06:00').split(':').map(Number);
    let time = `${String(Math.min(23, h + 1)).padStart(2, '0')}:00`;
    while (p.blocks.some((b) => b.time === time) && h < 23) time = `${String(++h).padStart(2, '0')}:00`;
    p.blocks.push({ time, targets: { ...(last?.targets ?? {}) } });
    p.blocks.sort((a, z) => a.time.localeCompare(z.time));
    this.touch();
  }

  async removeBlock(p: ScheduleProgram, i: number): Promise<void> {
    p.blocks.splice(i, 1);
    this.touch();
  }

  startTemplate(): void {
    const targets = Object.fromEntries(this.live.radiators().map((r) => [r.id, 16]));
    this.doc.set({
      enabled: false,
      programs: [
        { name: 'Weekdays', days: [0, 1, 2, 3, 4], blocks: [{ time: '00:00', targets: { ...targets } }] },
        { name: 'Weekend', days: [5, 6], blocks: [{ time: '00:00', targets: { ...targets } }] },
      ],
    });
    this.dirty.set(true);
  }

  // ------------------------------------------------------------ labels
  radiatorLabel(id: string): string {
    const r = this.live.radiators().find((x) => x.id === id);
    return r ? (r.area || r.name) : id;
  }

  /** Every radiator the dashboard knows plus any id the block schedules (e.g. a renamed one). */
  radiatorIds(b: ScheduleBlock): string[] {
    const ids = this.live.radiators().map((r) => r.id);
    return [...ids, ...Object.keys(b.targets).filter((id) => !ids.includes(id))];
  }

  daysLabel(days: number[]): string {
    return days.map((d) => DAY[d] ?? d).join(' · ');
  }

  endOf(p: ScheduleProgram, i: number): string {
    return p.blocks[(i + 1) % p.blocks.length].time;
  }
}
