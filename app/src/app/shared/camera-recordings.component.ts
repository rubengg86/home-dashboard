import { Component, OnInit, inject, input, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { IonButton, IonButtons, IonContent, IonHeader, IonIcon, IonTitle, IonToolbar, ModalController } from '@ionic/angular';
import { addIcons } from 'ionicons';
import { chevronBackOutline, chevronForwardOutline, refreshOutline } from 'ionicons/icons';
import { firstValueFrom } from 'rxjs';
import { AuthService } from '../core/auth.service';

interface RecHour { dir: string; start: string | null; day: string | null; label: string }
interface RecClip { name: string; path: string; start: string | null; label: string;
                    duration: number | null; size: number | null; thumb: string | null }
interface RecDay { day: string; hours: number[] }

/** Browse the camera's SD-card recordings: day and hour chips, clip grid with previews, inline player. */
@Component({
  selector: 'app-camera-recordings',
  imports: [IonHeader, IonToolbar, IonTitle, IonButtons, IonButton, IonIcon, IonContent],
  template: `
    <ion-header>
      <ion-toolbar>
        <ion-title>{{ name() }} · Recordings</ion-title>
        <ion-buttons slot="end">
          <ion-button (click)="load()" aria-label="Reload the list"><ion-icon slot="icon-only" name="refresh-outline"></ion-icon></ion-button>
          <ion-button (click)="close()">Close</ion-button>
        </ion-buttons>
      </ion-toolbar>
    </ion-header>
    <ion-content>
      @if (error(); as e) { <div class="note">Couldn't read the camera's recordings: {{ e }}</div> }
      @else if (!days().length) { <div class="note">{{ loading() ? 'Loading…' : 'No recordings on the camera yet.' }}</div> }
      @else {
        <div class="chips">
          @for (d of days(); track d.day; let k = $index) {
            <button class="chip day" [class.active]="k === dayIdx()" (click)="pickDay(k)">{{ dayLabel(d.day) }}</button>
          }
        </div>
        <div class="chips">
          @for (i of days()[dayIdx()].hours; track i) {
            <button class="chip" [class.active]="i === hourIdx()" (click)="selectHour(i)">{{ hours()[i].label }}</button>
          }
        </div>
        <div class="stage">
          @if (videoSrc(); as src) {
            <video [src]="src" controls autoplay playsinline webkit-playsinline (ended)="onEnded()"
                   (error)="playError.set(true)"></video>
          }
          @if (!videoSrc() || playError()) {
            <div class="hint">{{ playError() ? "This clip couldn't be played." : 'Pick a recording' }}</div>
          }
        </div>
        <div class="bar">
          <button class="chip" (click)="step(-1)" aria-label="Previous clip"><ion-icon name="chevron-back-outline"></ion-icon> Prev</button>
          <button class="chip" (click)="step(1)" aria-label="Next clip">Next <ion-icon name="chevron-forward-outline"></ion-icon></button>
          <span class="now">{{ nowLabel() }}</span>
          <button class="chip auto" [class.active]="auto()" (click)="auto.set(!auto())" aria-pressed="{{ auto() }}">Auto-play</button>
        </div>
        @if (clipsError(); as e) { <div class="note">{{ e }}</div> }
        @else if (!clips().length) { <div class="note">{{ loadingClips() ? 'Loading clips…' : 'No clips in this hour.' }}</div> }
        @else {
          <div class="grid">
            @for (c of clips(); track c.path; let k = $index) {
              <button class="clip" [class.active]="k === clipIdx()" (click)="play(k)" [attr.aria-label]="'Play the ' + c.label + ' clip'">
                <img loading="lazy" alt="" [src]="thumbSrc(c)" (error)="thumbError($event, c)">
                <span class="lbl">{{ c.label }}<span>{{ dur(c.duration) }}</span></span>
              </button>
            }
          </div>
        }
      }
    </ion-content>
  `,
  styles: [`
    ion-content { --background: #0d0d0c; --padding-start: 12px; --padding-end: 12px; --padding-top: 10px; --padding-bottom: 16px; }
    .note { color: #aaa; font-size: 13.5px; padding: 12px 4px; }
    .chips { display: flex; gap: 6px; overflow-x: auto; scrollbar-width: none; margin-bottom: 8px; }
    .chips::-webkit-scrollbar { display: none; }
    .chip { flex: none; display: inline-flex; align-items: center; gap: 4px; background: #ffffff14; color: #fff; border: 0;
            border-radius: 999px; padding: 7px 13px; font-size: 13px; font-family: inherit; }
    .chip.day { font-weight: 600; }
    .chip.active { background: #fff; color: #000; }
    .stage { position: relative; aspect-ratio: 16 / 9; background: #000; border-radius: 12px; overflow: hidden; margin: 4px 0 8px; }
    .stage video { position: absolute; inset: 0; width: 100%; height: 100%; background: #000; }
    .hint { position: absolute; inset: 0; display: grid; place-items: center; color: #999; font-size: 14px; pointer-events: none; }
    .bar { display: flex; align-items: center; gap: 6px; margin-bottom: 10px; }
    .bar ion-icon { font-size: 14px; }
    .now { flex: 1; min-width: 0; color: #ddd; font-size: 12.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(110px, 1fr)); gap: 8px; }
    .clip { position: relative; border: 0; border-radius: 10px; overflow: hidden; background: #ffffff14; color: #fff;
            padding: 0; text-align: left; aspect-ratio: 16 / 10; font-family: inherit; }
    .clip img { width: 100%; height: 100%; object-fit: cover; display: block; opacity: .9; }
    .clip .lbl { position: absolute; inset: auto 0 0 0; padding: 12px 7px 5px; font-size: 12px; font-weight: 600;
                 background: linear-gradient(transparent, #000b); display: flex; justify-content: space-between; }
    .clip .lbl span { font-weight: 400; opacity: .8; }
    .clip.active { outline: 2px solid var(--ion-color-primary); outline-offset: -2px; }
  `],
})
export class CameraRecordingsComponent implements OnInit {
  private auth = inject(AuthService);
  private http = inject(HttpClient);
  private modals = inject(ModalController);  // not "modal": Ionic assigns the host ion-modal element there
  camId = input.required<string>();
  name = input('');

  readonly hours = signal<RecHour[]>([]);
  readonly days = signal<RecDay[]>([]);
  readonly dayIdx = signal(0);
  readonly hourIdx = signal(-1);
  readonly clips = signal<RecClip[]>([]);
  readonly clipIdx = signal(-1);
  readonly loading = signal(true);
  readonly loadingClips = signal(false);
  readonly error = signal<string | null>(null);
  readonly clipsError = signal<string | null>(null);
  readonly videoSrc = signal<string | null>(null);
  readonly playError = signal(false);
  readonly auto = signal(true);
  private token = '';
  private seq = 0;

  constructor() { addIcons({ chevronBackOutline, chevronForwardOutline, refreshOutline }); }

  ngOnInit(): void { void this.load(); }

  private base(): string { return `/api/v1/cameras/${encodeURIComponent(this.camId())}/recordings`; }

  /** <img>/<video> can't send headers: media URLs carry a short-lived stream token. */
  private mediaURL(kind: 'file' | 'thumb', path: string): string {
    return `${this.auth.server()}${this.base()}/${kind}?path=${encodeURIComponent(path)}&token=${encodeURIComponent(this.token)}`;
  }

  thumbSrc(c: RecClip): string { return this.mediaURL(c.thumb ? 'file' : 'thumb', c.thumb || c.path); }

  /** A busy camera answers 503 for a while: retry a few times before hiding the tile. */
  thumbError(ev: Event, c: RecClip): void {
    const img = ev.target as HTMLImageElement;
    const n = +(img.dataset['try'] || 0) + 1;
    if (n > 3) { img.style.display = 'none'; return; }
    img.dataset['try'] = String(n);
    setTimeout(() => { if (img.isConnected) img.src = `${this.thumbSrc(c)}&r=${n}`; }, 5000 * n);
  }

  async load(): Promise<void> {
    const seq = ++this.seq;
    this.loading.set(true);
    this.error.set(null);
    this.videoSrc.set(null);
    this.playError.set(false);
    try {
      this.token = (await this.auth.streamToken()) ?? '';
      const { hours } = await firstValueFrom(this.http.get<{ hours: RecHour[] }>(this.base()));
      if (seq !== this.seq) return;
      this.hours.set(hours);
      const days: RecDay[] = [];  // hours are newest-first, so same-day folders are consecutive
      hours.forEach((h, i) => {
        const last = days[days.length - 1];
        if (last && last.day === (h.day || '')) last.hours.push(i);
        else days.push({ day: h.day || '', hours: [i] });
      });
      this.days.set(days);
      if (hours.length) await this.selectHour(0);
    } catch (e) {
      if (seq === this.seq) this.error.set(this.msg(e));
    } finally {
      if (seq === this.seq) this.loading.set(false);
    }
  }

  pickDay(k: number): void {
    if (k !== this.dayIdx()) void this.selectHour(this.days()[k].hours[0]);
  }

  async selectHour(i: number, then?: 'first' | 'last'): Promise<void> {
    const seq = ++this.seq;
    this.hourIdx.set(i);
    this.dayIdx.set(Math.max(0, this.days().findIndex((d) => d.hours.includes(i))));
    this.clips.set([]);
    this.clipIdx.set(-1);
    this.clipsError.set(null);
    this.loadingClips.set(true);
    try {
      const { clips } = await firstValueFrom(this.http.get<{ clips: RecClip[] }>(
        `${this.base()}?dir=${encodeURIComponent(this.hours()[i].dir)}`));
      if (seq !== this.seq) return;
      this.clips.set(clips);
      if (then === 'first' && clips.length) this.play(0);
      else if (then === 'last' && clips.length) this.play(clips.length - 1);
    } catch (e) {
      if (seq === this.seq) this.clipsError.set(`Couldn't list this hour: ${this.msg(e)}`);
    } finally {
      if (seq === this.seq) this.loadingClips.set(false);
    }
  }

  play(k: number): void {
    const c = this.clips()[k];
    if (!c) return;
    this.clipIdx.set(k);
    this.playError.set(false);
    this.videoSrc.set(this.mediaURL('file', c.path));
  }

  /** Forward in time crosses into the next (newer) hour: hours are listed newest-first. */
  step(dir: 1 | -1): void {
    if (this.clipIdx() < 0 && this.clips().length) return this.play(dir > 0 ? 0 : this.clips().length - 1);
    const k = this.clipIdx() + dir;
    if (k >= 0 && k < this.clips().length) return this.play(k);
    const h = this.hourIdx() - dir;
    if (h >= 0 && h < this.hours().length) void this.selectHour(h, dir > 0 ? 'first' : 'last');
  }

  onEnded(): void {
    if (this.auto()) this.step(1);
  }

  nowLabel(): string {
    const c = this.clips()[this.clipIdx()];
    if (!c) return '';
    const day = this.hours()[this.hourIdx()]?.day;
    return `${day ? this.dayLabel(day) + ' · ' : ''}${c.label}${c.duration ? ' · ' + this.dur(c.duration) : ''}`;
  }

  dayLabel(iso: string): string {
    if (!iso) return 'Other';
    const d = new Date(iso + 'T00:00:00');
    const today = new Date(); today.setHours(0, 0, 0, 0);
    const diff = Math.round((today.getTime() - d.getTime()) / 86400000);
    return diff === 0 ? 'Today' : diff === 1 ? 'Yesterday'
      : d.toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' });
  }

  dur(s: number | null): string {
    return s == null ? '' : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
  }

  private msg(e: unknown): string {
    const err = e as { error?: { detail?: string }; message?: string };
    return err?.error?.detail || err?.message || 'unknown error';
  }

  close(): void {
    this.videoSrc.set(null);  // aborts the stream so the camera's FTP connection closes
    void this.modals.dismiss();
  }
}
