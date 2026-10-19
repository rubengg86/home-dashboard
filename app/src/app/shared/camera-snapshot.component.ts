import { Component, OnDestroy, OnInit, inject, input, signal } from '@angular/core';
import { AuthService } from '../core/auth.service';

/** Camera still that refreshes every few seconds. <img> can't send headers: it uses a stream token. */
@Component({
  selector: 'app-camera-snapshot',
  template: `
    @if (src()) {
      <img [src]="src()" [alt]="name()" (load)="ok.set(true)" (error)="ok.set(false)" draggable="false" />
    }
    @if (!ok()) { <div class="msg">{{ src() ? 'Camera unavailable' : 'Loading…' }}</div> }
  `,
  styles: [`
    :host { display: block; position: relative; aspect-ratio: 16 / 9; background: #000; border-radius: var(--app-radius); overflow: hidden; }
    img { width: 100%; height: 100%; object-fit: cover; display: block; }
    .msg { position: absolute; inset: 0; display: grid; place-items: center; color: #aaa; font-size: 13px; }
  `],
})
export class CameraSnapshotComponent implements OnInit, OnDestroy {
  private auth = inject(AuthService);
  camId = input.required<string>();
  name = input('');
  seconds = input(3);
  src = signal<string | null>(null);
  ok = signal(true);
  private timer: ReturnType<typeof setInterval> | null = null;

  ngOnInit(): void {
    this.tick();
    this.timer = setInterval(() => this.tick(), this.seconds() * 1000);
  }

  ngOnDestroy(): void {
    if (this.timer) clearInterval(this.timer);
  }

  private async tick(): Promise<void> {
    if (document.hidden) return;
    const token = await this.auth.streamToken();
    if (!token) return;
    this.src.set(`${this.auth.server()}/api/v1/cameras/${encodeURIComponent(this.camId())}/snapshot` +
                 `?res=low&token=${encodeURIComponent(token)}&t=${Date.now()}`);
  }
}
