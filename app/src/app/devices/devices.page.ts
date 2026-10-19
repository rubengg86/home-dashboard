import { Component, computed, inject } from '@angular/core';
import {
  IonButton, IonButtons, IonContent, IonHeader, IonIcon, IonRange, IonRefresher, IonRefresherContent, IonTitle,
  IonToggle, IonToolbar,
} from '@ionic/angular';
import { addIcons } from 'ionicons';
import { bulb, bulbOutline, ellipseOutline, flame, gitNetworkOutline, lockClosedOutline, radioButtonOnOutline, refreshOutline } from 'ionicons/icons';
import { eventLabel, fmtKWh, fmtW, timeAgo } from '../core/format';
import { LiveService, sensor } from '../core/live.service';
import { DashEvent, Device, Heater } from '../core/models';
import { UiService } from '../shared/ui.service';

@Component({
  selector: 'app-devices',
  templateUrl: 'devices.page.html',
  styleUrls: ['devices.page.scss'],
  imports: [IonHeader, IonToolbar, IonTitle, IonButtons, IonButton, IonIcon, IonContent, IonRefresher,
            IonRefresherContent, IonToggle, IonRange],
})
export class DevicesPage {
  live = inject(LiveService);
  private ui = inject(UiService);
  readonly fmtW = fmtW; readonly fmtKWh = fmtKWh; readonly eventLabel = eventLabel; readonly timeAgo = timeAgo;

  readonly lightsOn = computed(() => this.live.lights().filter((d) => d.on).length);
  readonly plugsOn = computed(() => this.live.plugs().filter((d) => d.on).length);
  /** Latest event per device (button presses). */
  readonly lastEvent = computed(() => {
    const m = new Map<string, DashEvent>();
    for (const e of this.live.events()) if (!m.has(e.device_id)) m.set(e.device_id, e);
    return m;
  });

  constructor() {
    addIcons({ bulb, bulbOutline, flame, gitNetworkOutline, lockClosedOutline, radioButtonOnOutline, ellipseOutline, refreshOutline });
  }

  power(d: Device): number | null {
    const p = sensor(d, 'current_consumption');
    return typeof p === 'number' ? p : null;
  }

  today(d: Device): number | null {
    const v = sensor(d, 'consumption_today');
    return typeof v === 'number' ? v : null;
  }

  async toggle(d: Device, ev: CustomEvent): Promise<void> {
    const on = ev.detail.checked as boolean;
    const toggle = ev.target as HTMLIonToggleElement;
    if (!on && d.confirm_off &&
        !await this.ui.confirm(`Switch off ${d.alias}?`, 'This device is protected so it isn’t switched off by accident.', 'Switch off')) {
      toggle.checked = true;  // cancelled: put the switch back
      return;
    }
    await this.ui.run(() => this.live.setDevice(d, on));
  }

  async bright(d: Device, ev: CustomEvent): Promise<void> {
    await this.ui.run(() => this.live.setBrightness(d, ev.detail.value as number));
  }

  async heater(h: Heater, ev: CustomEvent): Promise<void> {
    await this.ui.run(() => this.live.setHeater(h, ev.detail.checked));
  }

  async rediscover(): Promise<void> {
    this.ui.toast('Searching the network…');
    await this.ui.run(async () => this.ui.toast(`${await this.live.discover()} device(s) found`));
  }

  async doRefresh(ev: CustomEvent): Promise<void> {
    await this.live.refresh();
    (ev.target as HTMLIonRefresherElement).complete();
  }
}
