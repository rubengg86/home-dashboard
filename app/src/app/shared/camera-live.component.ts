import { Component, OnDestroy, OnInit, inject, input, signal } from '@angular/core';
import { IonIcon, ModalController } from '@ionic/angular';
import { addIcons } from 'ionicons';
import { closeOutline, filmOutline, phoneLandscapeOutline } from 'ionicons/icons';
import { AuthService } from '../core/auth.service';
import { CameraRecordingsComponent } from './camera-recordings.component';

/** Full-screen live video: the server turns RTSP into MJPEG, which a plain <img> plays.
 *  On a portrait phone the picture is rotated to landscape so it fills the screen. */
@Component({
  selector: 'app-camera-live',
  imports: [IonIcon],
  template: `
    <div class="wrap" [class.rot]="rotated()">
      @if (src()) { <img [src]="src()" [alt]="name() + ' live'" (error)="failed.set(true)" (load)="failed.set(false)" /> }
      @if (failed()) { <div class="msg">Live video unavailable</div> }
      <div class="ui">
        <span class="ttl">{{ name() }}</span>
        <span class="grow"></span>
        <button class="cbtn txt" (click)="setQuality(quality() === 'hd' ? 'sd' : 'hd')"
                [attr.aria-label]="'Quality: ' + quality().toUpperCase() + ', tap to switch'">{{ quality().toUpperCase() }}</button>
        @if (recordings()) {
          <button class="cbtn" (click)="openRecordings()" aria-label="Recordings"><ion-icon name="film-outline"></ion-icon></button>
        }
        <button class="cbtn" (click)="rotated.set(!rotated())" aria-label="Rotate the picture">
          <ion-icon name="phone-landscape-outline"></ion-icon>
        </button>
        <button class="cbtn" (click)="close()" aria-label="Close"><ion-icon name="close-outline"></ion-icon></button>
      </div>
    </div>
  `,
  styles: [`
    :host { display: block; position: absolute; inset: 0; background: #000; overflow: hidden; }
    .wrap { position: absolute; inset: 0; }
    .wrap img { width: 100%; height: 100%; object-fit: contain; display: block; }
    /* landscape on a portrait phone: lay the whole view (picture + controls) on its side */
    .wrap.rot { inset: auto; top: 0; left: 0; width: 100dvh; height: 100dvw;
                transform: rotate(90deg) translateY(-100%); transform-origin: top left; }
    .msg { position: absolute; inset: 0; display: grid; place-items: center; color: #aaa; }
    .ui { position: absolute; top: 0; left: 0; right: 0; display: flex; align-items: center; gap: 10px;
          padding: max(10px, env(safe-area-inset-top)) 14px 14px; color: #fff;
          background: linear-gradient(rgba(0, 0, 0, .55), transparent); }
    .rot .ui { padding-top: 10px; }
    .ttl { font-weight: 600; font-size: 15px; text-shadow: 0 1px 2px #000; }
    .grow { flex: 1; }
    .cbtn { width: 38px; height: 38px; border-radius: 50%; border: 0; background: #ffffff22; color: #fff;
            display: grid; place-items: center; font-family: inherit; }
    .cbtn ion-icon { font-size: 19px; }
    .cbtn.txt { width: auto; border-radius: 999px; padding: 0 13px; font-size: 12.5px; font-weight: 700; }
  `],
})
export class CameraLiveComponent implements OnInit, OnDestroy {
  private auth = inject(AuthService);
  private modals = inject(ModalController);  // not "modal": Ionic assigns the host ion-modal element there
  camId = input.required<string>();
  name = input('');
  recordings = input(false);
  quality = signal<'hd' | 'sd'>('hd');
  src = signal<string | null>(null);
  failed = signal(false);
  rotated = signal(window.innerHeight > window.innerWidth);
  private onResize = () => this.rotated.set(window.innerHeight > window.innerWidth);

  constructor() { addIcons({ closeOutline, filmOutline, phoneLandscapeOutline }); }

  ngOnInit(): void {
    window.addEventListener('resize', this.onResize);  // the OS rotated for us: undo ours
    void this.load();
  }

  ngOnDestroy(): void {
    window.removeEventListener('resize', this.onResize);
  }

  setQuality(q: 'hd' | 'sd'): void {
    this.quality.set(q);
    void this.load();
  }

  async load(): Promise<void> {
    const token = await this.auth.streamToken();
    this.src.set(null);  // drop the old stream so the server can stop it
    if (!token) return;
    this.src.set(`${this.auth.server()}/api/v1/cameras/${encodeURIComponent(this.camId())}/live` +
                 `?q=${this.quality()}&token=${encodeURIComponent(token)}`);
  }

  async openRecordings(): Promise<void> {
    this.src.set(null);  // pause the live stream (frees the server's ffmpeg) while browsing
    const m = await this.modals.create({
      component: CameraRecordingsComponent,
      componentProps: { camId: this.camId(), name: this.name() },
    });
    await m.present();
    await m.onDidDismiss();
    void this.load();
  }

  close(): void {
    this.src.set(null);
    void this.modals.dismiss();
  }
}
