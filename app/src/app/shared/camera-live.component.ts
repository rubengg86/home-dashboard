import { Component, OnInit, inject, input, signal } from '@angular/core';
import { IonButton, IonButtons, IonContent, IonHeader, IonSegment, IonSegmentButton, IonLabel, IonTitle, IonToolbar, ModalController } from '@ionic/angular';
import { FormsModule } from '@angular/forms';
import { AuthService } from '../core/auth.service';

/** Full-screen live video: the server turns RTSP into MJPEG, which a plain <img> plays. */
@Component({
  selector: 'app-camera-live',
  imports: [FormsModule, IonHeader, IonToolbar, IonTitle, IonButtons, IonButton, IonContent, IonSegment, IonSegmentButton, IonLabel],
  template: `
    <ion-header>
      <ion-toolbar>
        <ion-title>{{ name() }}</ion-title>
        <ion-buttons slot="end"><ion-button (click)="close()">Close</ion-button></ion-buttons>
      </ion-toolbar>
    </ion-header>
    <ion-content class="ion-padding">
      <ion-segment [(ngModel)]="quality" (ionChange)="load()">
        <ion-segment-button value="hd"><ion-label>HD</ion-label></ion-segment-button>
        <ion-segment-button value="sd"><ion-label>SD</ion-label></ion-segment-button>
      </ion-segment>
      <div class="stage">
        @if (src()) { <img [src]="src()" [alt]="name() + ' live'" (error)="failed.set(true)" (load)="failed.set(false)" /> }
        @if (failed()) { <div class="msg">Live video unavailable</div> }
      </div>
    </ion-content>
  `,
  styles: [`
    ion-content { --background: #000; }
    ion-segment { margin-bottom: 16px; }
    .stage { position: relative; aspect-ratio: 16 / 9; background: #111; border-radius: 12px; overflow: hidden; }
    img { width: 100%; height: 100%; object-fit: contain; }
    .msg { position: absolute; inset: 0; display: grid; place-items: center; color: #aaa; }
  `],
})
export class CameraLiveComponent implements OnInit {
  private auth = inject(AuthService);
  private modal = inject(ModalController);
  camId = input.required<string>();
  name = input('');
  quality = 'hd';
  src = signal<string | null>(null);
  failed = signal(false);

  ngOnInit(): void { this.load(); }

  async load(): Promise<void> {
    const token = await this.auth.streamToken();
    this.src.set(null);  // drop the old stream so the server can stop it
    if (!token) return;
    this.src.set(`${this.auth.server()}/api/v1/cameras/${encodeURIComponent(this.camId())}/live` +
                 `?q=${this.quality}&token=${encodeURIComponent(token)}`);
  }

  close(): void {
    this.src.set(null);
    this.modal.dismiss();
  }
}
