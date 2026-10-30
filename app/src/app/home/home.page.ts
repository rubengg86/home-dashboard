import { Component, computed, inject, signal } from '@angular/core';
import { Router } from '@angular/router';
import {
  ActionSheetController, IonButton, IonButtons, IonContent, IonHeader, IonIcon, IonRefresher, IonRefresherContent,
  IonTitle, IonToggle, IonToolbar, ModalController,
} from '@ionic/angular';
import { addIcons } from 'ionicons';
import { flame, personCircleOutline, powerOutline, shirtOutline, videocamOutline, waterOutline } from 'ionicons/icons';
import { AuthService } from '../core/auth.service';
import { eventLabel, fmtKWh, fmtMoney, fmtW, hhmm, minutesUntil, timeAgo } from '../core/format';
import { LiveService } from '../core/live.service';
import { Camera, Heater } from '../core/models';
import { CameraLiveComponent } from '../shared/camera-live.component';
import { CameraSnapshotComponent } from '../shared/camera-snapshot.component';
import { UiService } from '../shared/ui.service';
import { BUILD } from '../../environments/version';

@Component({
  selector: 'app-home',
  templateUrl: 'home.page.html',
  styleUrls: ['home.page.scss'],
  imports: [IonHeader, IonToolbar, IonTitle, IonButtons, IonButton, IonIcon, IonContent, IonRefresher,
            IonRefresherContent, IonToggle, CameraSnapshotComponent],
})
export class HomePage {
  readonly build = BUILD;
  live = inject(LiveService);
  private auth = inject(AuthService);
  private ui = inject(UiService);
  private router = inject(Router);
  private sheets = inject(ActionSheetController);
  private modals = inject(ModalController);
  readonly fmtW = fmtW; readonly fmtKWh = fmtKWh; readonly fmtMoney = fmtMoney;
  readonly hhmm = hhmm; readonly eventLabel = eventLabel; readonly timeAgo = timeAgo; readonly minutesUntil = minutesUntil;

  private now = signal(Date.now());
  readonly greeting = computed(() => {
    const h = new Date(this.now()).getHours();
    return h < 5 ? 'Good night' : h < 12 ? 'Good morning' : h < 20 ? 'Good afternoon' : 'Good evening';
  });

  /** Today's kWh across every metered device, from the energy history (last daily bucket). */
  readonly today = computed(() => {
    const e = this.live.energy();
    if (!e) return null;
    const r = e.ranges['7d'], last = r.buckets.length - 1;
    const kwh = e.series.reduce((a, s) => a + (r.values[s.id]?.[last] ?? 0), 0);
    return { kwh, cost: e.price > 0 ? kwh * e.price : null };
  });

  readonly onCount = computed(() => this.live.allDevices().filter((d) => d.switchable && d.on).length);
  readonly running = computed(() => this.live.appliances().appliances.filter((a) => a.status !== 'Off'));

  constructor() {
    addIcons({ flame, personCircleOutline, powerOutline, shirtOutline, videocamOutline, waterOutline });
    setInterval(() => this.now.set(Date.now()), 30_000);  // keeps "x min left" fresh
  }

  async doRefresh(ev: CustomEvent): Promise<void> {
    await Promise.all([this.live.refresh(), this.live.loadEnergy()]);
    (ev.target as HTMLIonRefresherElement).complete();
  }

  heaterLabel(h: Heater): string {
    return h.status === 'Hot' ? 'Hot water ready' : h.status;
  }

  boostLeft(h: Heater): number | null {
    this.now();
    return h.on ? minutesUntil(h.boost_ends_at) : null;
  }

  async toggleHeater(h: Heater, on: boolean): Promise<void> {
    await this.ui.run(() => this.live.setHeater(h, on));
  }

  async boost(h: Heater, minutes: number): Promise<void> {
    await this.ui.run(async () => {
      await this.live.boost(h, minutes);
      this.ui.toast(minutes ? `Boost ${minutes} min started` : 'Boost cancelled');
    });
  }

  async allOff(): Promise<void> {
    if (!await this.ui.confirm('Switch everything off?', 'Protected devices like the fridge plug stay on.', 'All off')) return;
    await this.ui.run(async () => this.ui.toast(`Switched off ${await this.live.allOff()} device(s)`));
  }

  async openCamera(c: Camera): Promise<void> {
    const m = await this.modals.create({ component: CameraLiveComponent, componentProps: { camId: c.id, name: c.name } });
    await m.present();
  }

  async account(): Promise<void> {
    const s = await this.sheets.create({
      header: this.auth.server(),
      buttons: [
        { text: 'Sign out', role: 'destructive', handler: () => { this.signOut(); } },
        { text: 'Cancel', role: 'cancel' },
      ],
    });
    await s.present();
  }

  private async signOut(): Promise<void> {
    await this.auth.logout();
    await this.router.navigateByUrl('/login', { replaceUrl: true });
  }
}
