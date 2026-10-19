import { Injectable, inject } from '@angular/core';
import { AlertController, ToastController } from '@ionic/angular';
import { errorText } from '../core/live.service';

@Injectable({ providedIn: 'root' })
export class UiService {
  private alerts = inject(AlertController);
  private toasts = inject(ToastController);

  /** Resolves true only when the user taps the confirm button. */
  async confirm(header: string, message: string, ok = 'OK', destructive = true): Promise<boolean> {
    const a = await this.alerts.create({
      header, message,
      buttons: [{ text: 'Cancel', role: 'cancel' }, { text: ok, role: destructive ? 'destructive' : 'confirm' }],
    });
    await a.present();
    const { role } = await a.onDidDismiss();
    return role === 'destructive' || role === 'confirm';
  }

  async toast(message: string, error = false): Promise<void> {
    const t = await this.toasts.create({ message, duration: 2200, position: 'top', color: error ? 'danger' : 'dark' });
    await t.present();
  }

  /** Runs a command and turns a failure into a toast. */
  async run(fn: () => Promise<unknown>): Promise<void> {
    try { await fn(); } catch (e) { this.toast(errorText(e), true); }
  }
}
