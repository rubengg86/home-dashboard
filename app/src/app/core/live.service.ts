import { HttpClient } from '@angular/common/http';
import { Injectable, computed, inject, signal } from '@angular/core';
import { App } from '@capacitor/app';
import { firstValueFrom } from 'rxjs';
import { AuthService } from './auth.service';
import { Appliances, Cameras, DashEvent, Device, Energy, Heater, Overview, Sensor } from './models';

/**
 * The app's single source of truth: loads /overview, then keeps it current from the WebSocket
 * (same messages the web dashboard gets). Commands go over HTTP; the WebSocket confirms them.
 */
@Injectable({ providedIn: 'root' })
export class LiveService {
  private http = inject(HttpClient);
  private auth = inject(AuthService);

  readonly devices = signal<Device[]>([]);
  readonly heaters = signal<Heater[]>([]);
  readonly appliances = signal<Appliances>({ error: null, appliances: [] });
  readonly events = signal<DashEvent[]>([]);
  readonly cameras = signal<Cameras>({ refresh_seconds: 2, live: false, cameras: [] });
  readonly energy = signal<Energy | null>(null);
  readonly price = signal(0);
  readonly currency = signal('€');
  readonly connected = signal(false);
  readonly loaded = signal(false);
  readonly error = signal<string | null>(null);

  /** Every device incl. hub children, flat. */
  readonly allDevices = computed(() => this.devices().flatMap((d) => [d, ...(d.children ?? [])]));
  readonly hubs = computed(() => this.devices().filter((d) => d.children?.length));
  readonly lights = computed(() => this.devices().filter((d) => !d.children?.length && isLight(d)));
  readonly plugs = computed(() => this.devices().filter((d) => !d.children?.length && !isLight(d) && d.switchable));

  /** Live power of everything that meters it (plugs + heater), W. */
  readonly powerNow = computed(() => {
    let w = 0, any = false;
    for (const d of this.allDevices()) {
      const p = sensor(d, 'current_consumption');
      if (typeof p === 'number') { w += p; any = true; }
    }
    for (const h of this.heaters()) {
      if (h.online) { w += h.on ? h.power_w ?? 0 : 0; any = true; }
    }
    return any ? w : null;
  });

  private ws: WebSocket | null = null;
  private retry = 0;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private energyTimer: ReturnType<typeof setInterval> | null = null;
  private started = false;

  async start(): Promise<void> {
    if (this.started) return;
    this.started = true;
    await this.refresh();
    this.connect();
    this.loadEnergy();
    this.energyTimer = setInterval(() => this.loadEnergy(), 5 * 60_000);
    // phones suspend sockets in the background: drop it there, reconnect + resync on resume
    App.addListener('appStateChange', ({ isActive }) => {
      if (!this.started) return;
      if (isActive) { this.refresh(); this.loadEnergy(); this.connect(); }
      else this.disconnect();
    });
  }

  stop(): void {
    this.started = false;
    this.disconnect();
    if (this.energyTimer) clearInterval(this.energyTimer);
    App.removeAllListeners();
    this.loaded.set(false);
  }

  async refresh(): Promise<void> {
    try {
      const o = await firstValueFrom(this.http.get<Overview>('/api/v1/overview'));
      this.devices.set(o.devices);
      this.heaters.set(o.heaters);
      this.appliances.set(o.appliances);
      this.events.set(o.events);
      this.cameras.set(o.cameras);
      this.price.set(o.price_per_kwh);
      this.currency.set(o.currency);
      this.error.set(null);
      this.loaded.set(true);
    } catch (e) {
      this.error.set(errorText(e));
    }
  }

  async loadEnergy(): Promise<void> {
    try {
      this.energy.set(await firstValueFrom(this.http.get<Energy>('/api/v1/energy')));
    } catch { /* keep the last data */ }
  }

  // ------------------------------------------------------------ websocket
  private async connect(): Promise<void> {
    if (!this.started || (this.ws && this.ws.readyState <= WebSocket.OPEN)) return;
    const token = await this.auth.streamToken();
    if (!token) return this.scheduleReconnect();
    const url = this.auth.server().replace(/^http/, 'ws') + `/api/v1/ws?token=${encodeURIComponent(token)}`;
    const ws = new WebSocket(url);
    this.ws = ws;
    let ping: ReturnType<typeof setInterval>;
    ws.onopen = () => {
      this.connected.set(true);
      this.retry = 0;
      ping = setInterval(() => ws.readyState === WebSocket.OPEN && ws.send('ping'), 25_000);
    };
    ws.onmessage = (m) => this.onMessage(JSON.parse(m.data));
    ws.onclose = () => {
      clearInterval(ping);
      this.connected.set(false);
      if (this.ws === ws) { this.ws = null; this.scheduleReconnect(); }
    };
  }

  private disconnect(): void {
    if (this.retryTimer) clearTimeout(this.retryTimer);
    const ws = this.ws;
    this.ws = null;
    ws?.close();
    this.connected.set(false);
  }

  private scheduleReconnect(): void {
    if (!this.started) return;
    if (this.retryTimer) clearTimeout(this.retryTimer);
    const delay = Math.min(30_000, 1000 * 2 ** this.retry++);
    this.retryTimer = setTimeout(() => this.connect(), delay);
  }

  private onMessage(msg: any): void {
    switch (msg.type) {
      case 'devices': this.devices.set(msg.devices); break;
      case 'tuya': this.heaters.set(msg.devices); break;
      case 'appliances': this.appliances.set({ error: msg.error, auth: msg.auth, appliances: msg.appliances }); break;
      case 'events': this.events.set(msg.events); break;
      case 'event': this.events.update((e) => [msg.event, ...e].slice(0, 50)); break;
    }
  }

  // ------------------------------------------------------------ commands (optimistic, server confirms over WS)
  async setDevice(d: Device, on: boolean): Promise<void> {
    this.patchDevice(d.id, { on });
    try {
      await firstValueFrom(this.http.post(`/api/v1/devices/${encodeURIComponent(d.id)}/state`, { on }));
    } catch (e) {
      this.patchDevice(d.id, { on: !on });
      throw e;
    }
  }

  async setBrightness(d: Device, value: number): Promise<void> {
    this.patchDevice(d.id, { brightness: value, on: true });
    await firstValueFrom(this.http.post(`/api/v1/devices/${encodeURIComponent(d.id)}/brightness`, { value }));
  }

  async setHeater(h: Heater, on: boolean): Promise<void> {
    this.heaters.update((list) => list.map((x) => (x.id === h.id ? { ...x, on } : x)));
    await firstValueFrom(this.http.post(`/api/v1/tuya/${encodeURIComponent(h.id)}/state`, { on }));
  }

  async boost(h: Heater, minutes: number): Promise<void> {
    await firstValueFrom(this.http.post(`/api/v1/tuya/${encodeURIComponent(h.id)}/boost`, { minutes }));
  }

  async allOff(): Promise<number> {
    const r = await firstValueFrom(this.http.post<{ switched_off: number }>('/api/v1/scenes/all-off', {}));
    return r.switched_off;
  }

  async discover(): Promise<number> {
    const r = await firstValueFrom(this.http.post<{ devices: number }>('/api/v1/discover', {}));
    return r.devices;
  }

  private patchDevice(id: string, patch: Partial<Device>): void {
    const apply = (d: Device): Device => d.id === id ? { ...d, ...patch } : d.children ? { ...d, children: d.children.map(apply) } : d;
    this.devices.update((list) => list.map(apply));
  }
}

export function isLight(d: Device): boolean {
  return /bulb|light|strip|dimmer/.test(d.type || '') || (d.model || '').toUpperCase().startsWith('L');
}

export function sensor(d: Device, id: string): Sensor['value'] | undefined {
  return d.sensors?.find((s) => s.id === id)?.value;
}

export function errorText(e: unknown): string {
  const err = e as { status?: number; error?: { detail?: string }; message?: string };
  if (err?.status === 0) return "Can't reach the server";
  return err?.error?.detail || err?.message || 'Something went wrong';
}

