import { Component, computed, inject, signal } from '@angular/core';
import { Router } from '@angular/router';
import {
  ActionSheetController, IonButton, IonButtons, IonContent, IonHeader, IonIcon, IonRefresher, IonRefresherContent,
  IonTitle, IonToggle, IonToolbar, ModalController,
} from '@ionic/angular';
import { addIcons } from 'ionicons';
import {
  chevronDownOutline, chevronUpOutline, flame, personCircleOutline, shirtOutline,
  thermometerOutline, videocamOutline, waterOutline,
} from 'ionicons/icons';
import { AuthService } from '../core/auth.service';
import { eventLabel, fmtTemp, hhmm, minutesUntil, timeAgo } from '../core/format';
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
  readonly fmtTemp = fmtTemp; readonly hhmm = hhmm;
  readonly eventLabel = eventLabel; readonly timeAgo = timeAgo; readonly minutesUntil = minutesUntil;
  readonly showEvents = signal(false);

  private now = signal(Date.now());
  readonly greeting = computed(() => {
    const h = new Date(this.now()).getHours();
    return h < 5 ? 'Good night' : h < 12 ? 'Good morning' : h < 20 ? 'Good afternoon' : 'Good evening';
  });

  readonly running = computed(() => this.live.appliances().appliances.filter((a) => a.status !== 'Off'));

  constructor() {
    addIcons({ chevronDownOutline, chevronUpOutline, flame, personCircleOutline, shirtOutline,
               thermometerOutline, videocamOutline, waterOutline });
    setInterval(() => this.now.set(Date.now()), 30_000);  // keeps "x min left" fresh
  }

  async doRefresh(ev: CustomEvent): Promise<void> {
    await this.live.refresh();
    (ev.target as HTMLIonRefresherElement).complete();
  }

  heaterLabel(h: Heater): string {
    return h.status === 'Hot' ? 'Hot water ready' : h.status;
  }

  async toggleHeater(h: Heater, on: boolean): Promise<void> {
    await this.ui.run(() => this.live.setHeater(h, on));
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
