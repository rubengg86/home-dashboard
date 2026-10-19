import { HttpBackend, HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Injectable, computed, inject, signal } from '@angular/core';
import { Capacitor } from '@capacitor/core';
import { Preferences } from '@capacitor/preferences';
import { firstValueFrom } from 'rxjs';
import { environment } from '../../environments/environment';
import { Tokens } from './models';

interface Session {
  server: string;           // e.g. http://192.168.13.173:8811
  accessToken: string;
  accessExpiresAt: number;  // ms epoch
  refreshToken: string;
  sessionId: string;
}

const KEY = 'session';
const LAST_SERVER = 'server';

/**
 * Tokens for the dashboard API. Access token (1 h) goes on every request; the refresh token is
 * single use, so every refresh stores the new one. Concurrent 401s share one refresh.
 */
@Injectable({ providedIn: 'root' })
export class AuthService {
  // raw client: no interceptor, so refresh/login can't loop through it
  private http = new HttpClient(inject(HttpBackend));
  private session = signal<Session | null>(null);
  private refreshing: Promise<boolean> | null = null;
  private stream: { token: string; expiresAt: number } | null = null;

  readonly loggedIn = computed(() => !!this.session());
  readonly server = computed(() => this.session()?.server ?? '');
  private ready: Promise<void>;

  constructor() {
    this.ready = Preferences.get({ key: KEY }).then(({ value }) => {
      if (value) this.session.set(JSON.parse(value));
    });
  }

  whenReady(): Promise<void> {
    return this.ready;
  }

  async lastServer(): Promise<string> {
    return (await Preferences.get({ key: LAST_SERVER })).value || environment.defaultServer;
  }

  accessToken(): string | null {
    return this.session()?.accessToken ?? null;
  }

  /** Access token, refreshed first when it's about to expire. */
  async freshAccessToken(): Promise<string | null> {
    await this.ready;
    const s = this.session();
    if (!s) return null;
    if (s.accessExpiresAt - Date.now() < 60_000) await this.refresh();
    return this.accessToken();
  }

  async login(server: string, username: string, password: string): Promise<void> {
    server = normalizeServer(server);
    const t = await firstValueFrom(this.http.post<Tokens>(`${server}/api/v1/auth/login`, {
      username, password, device_name: deviceName(),
    }));
    await Preferences.set({ key: LAST_SERVER, value: server });
    await this.store(server, t);
  }

  /** Swap the refresh token. False = the session is gone (logged out elsewhere, password changed...). */
  refresh(): Promise<boolean> {
    if (this.refreshing) return this.refreshing;
    this.refreshing = (async () => {
      const s = this.session();
      if (!s) return false;
      try {
        const t = await firstValueFrom(this.http.post<Tokens>(`${s.server}/api/v1/auth/refresh`,
          { refresh_token: s.refreshToken }));
        await this.store(s.server, t);
        return true;
      } catch (e) {
        // a network error keeps the session (offline); a 401 means it's over
        if (e instanceof HttpErrorResponse && e.status === 401) await this.clear();
        return false;
      } finally {
        this.refreshing = null;
      }
    })();
    return this.refreshing;
  }

  /** 15-min read-only token for camera <img>/<video> URLs and the WebSocket. */
  async streamToken(): Promise<string | null> {
    if (this.stream && this.stream.expiresAt - Date.now() > 60_000) return this.stream.token;
    const s = this.session();
    const access = await this.freshAccessToken();
    if (!s || !access) return null;
    try {
      const r = await firstValueFrom(this.http.get<{ token: string; expires_in: number }>(
        `${s.server}/api/v1/auth/stream-token`, { headers: { Authorization: `Bearer ${access}` } }));
      this.stream = { token: r.token, expiresAt: Date.now() + r.expires_in * 1000 };
      return r.token;
    } catch {
      return null;
    }
  }

  async logout(): Promise<void> {
    const s = this.session();
    if (s) {
      // best effort: end the session on the server too
      this.http.post(`${s.server}/api/v1/auth/logout`, {}, { headers: { Authorization: `Bearer ${s.accessToken}` } })
        .subscribe({ error: () => undefined });
    }
    await this.clear();
  }

  private async store(server: string, t: Tokens): Promise<void> {
    const s: Session = { server, accessToken: t.access_token, accessExpiresAt: Date.now() + t.expires_in * 1000,
                         refreshToken: t.refresh_token, sessionId: t.session_id };
    this.session.set(s);
    await Preferences.set({ key: KEY, value: JSON.stringify(s) });
  }

  private async clear(): Promise<void> {
    this.session.set(null);
    this.stream = null;
    await Preferences.remove({ key: KEY });
  }
}

export function normalizeServer(raw: string): string {
  let s = raw.trim().replace(/\/+$/, '');
  if (!/^https?:\/\//i.test(s)) s = `http://${s}`;
  return s;
}

function deviceName(): string {
  const p = Capacitor.getPlatform();
  return p === 'ios' ? 'iPhone app' : p === 'android' ? 'Android app' : 'Web app';
}
