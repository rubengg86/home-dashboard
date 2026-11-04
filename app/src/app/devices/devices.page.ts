import { Component, computed, inject, signal } from '@angular/core';
import {
  IonButton, IonButtons, IonContent, IonHeader, IonIcon, IonRange, IonRefresher, IonRefresherContent, IonTitle,
  IonToggle, IonToolbar,
} from '@ionic/angular';
import { addIcons } from 'ionicons';
import {
  bulb, bulbOutline, flame, gitNetworkOutline, lockClosedOutline, radioButtonOnOutline,
  refreshOutline, thermometerOutline, waterOutline,
} from 'ionicons/icons';
import { fmtKWh, fmtMoney, fmtTemp, fmtW, hhmm, minutesUntil } from '../core/format';
import { LiveService, isLight, sensor } from '../core/live.service';
import { Device, Heater } from '../core/models';
import { UiService } from '../shared/ui.service';

/** One room section: its devices sorted lights → plugs → sensors/buttons. */
interface RoomGroup {
  name: string;
  devices: Device[];
  on: number;          // switchable devices currently on
  switchable: number;  // switchable devices in the room
}

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
  readonly fmtW = fmtW; readonly fmtKWh = fmtKWh; readonly fmtMoney = fmtMoney; readonly fmtTemp = fmtTemp;
  readonly hhmm = hhmm; readonly isLight = isLight;
  private now = signal(Date.now());

  readonly lightsOn = computed(() => this.live.lights().filter((d) => d.on).length);
  readonly plugsOn = computed(() => this.live.plugs().filter((d) => d.on).length);

  /** Devices grouped by their Tapo room (rooms.json on the server), hub children included.
   *  Buttons (not switchable, no readings) are hidden: they do their job without a tile.
   *  null when no device has a room, so the template keeps the flat Lights/Plugs layout. */
  readonly roomGroups = computed<RoomGroup[] | null>(() => {
    const all = this.live.allDevices()
      .filter((d) => d.type !== 'hub' && (d.switchable || this.temp(d) != null));
    if (!all.some((d) => d.room)) return null;
    const rank = (d: Device) => this.temp(d) != null ? 2 : isLight(d) ? 0 : 1;
    const byRoom = new Map<string, Device[]>();
    for (const d of all) {
      const room = d.room || 'Other';
      if (!byRoom.has(room)) byRoom.set(room, []);
      byRoom.get(room)!.push(d);
    }
    return [...byRoom.entries()]
      .sort(([a], [b]) => +(a === 'Other') - +(b === 'Other') || a.localeCompare(b))
      .map(([name, devices]) => ({
        name,
        devices: devices.sort((a, b) => rank(a) - rank(b) || a.alias.localeCompare(b.alias)),
        on: devices.filter((d) => d.switchable && d.on).length,
        switchable: devices.filter((d) => d.switchable).length,
      }));
  });

  constructor() {
    addIcons({ bulb, bulbOutline, flame, gitNetworkOutline, lockClosedOutline, radioButtonOnOutline,
               refreshOutline, thermometerOutline, waterOutline });
    setInterval(() => this.now.set(Date.now()), 30_000);  // keeps "x min left" fresh
  }

  heaterLabel(h: Heater): string {
    return h.status === 'Hot' ? 'Hot water ready' : h.status;
  }

  boostLeft(h: Heater): number | null {
    this.now();
    return h.on ? minutesUntil(h.boost_ends_at) : null;
  }

  async boost(h: Heater, minutes: number): Promise<void> {
    await this.ui.run(async () => {
      await this.live.boost(h, minutes);
      this.ui.toast(minutes ? `Boost ${minutes} min started` : 'Boost cancelled');
    });
  }

  /** One-line status for a device row: state, brightness, live power, today's kWh. */
  rowMeta(d: Device): string {
    if (!d.online) return 'Offline';
    if (this.temp(d) != null) {
      const h = this.humidity(d);
      return h != null ? `${h}% humidity` : d.model;
    }
    if (!d.on) return 'Off';
    const parts = [d.dimmable ? `On · ${d.brightness}%` : 'On'];
    const p = this.power(d);
    if (p != null) parts.push(fmtW(p));
    const t = this.today(d);
    if (t != null) parts.push(`${fmtKWh(t)} today`);
    return parts.join(' · ');
  }

  power(d: Device): number | null {
    const p = sensor(d, 'current_consumption');
    return typeof p === 'number' ? p : null;
  }

  today(d: Device): number | null {
    const v = sensor(d, 'consumption_today');
    return typeof v === 'number' ? v : null;
  }

  temp(d: Device): number | null {
    const v = sensor(d, 'temperature');
    return typeof v === 'number' ? v : null;
  }

  humidity(d: Device): number | null {
    const v = sensor(d, 'humidity');
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
