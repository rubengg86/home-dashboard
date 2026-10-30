import { Component, OnInit, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { IonButton, IonContent, IonIcon, IonInput, IonInputPasswordToggle, IonSpinner, IonText } from '@ionic/angular';
import { addIcons } from 'ionicons';
import { flash, serverOutline } from 'ionicons/icons';
import { AuthService } from '../core/auth.service';
import { BUILD } from '../../environments/version';

@Component({
  selector: 'app-login',
  templateUrl: 'login.page.html',
  styleUrls: ['login.page.scss'],
  imports: [FormsModule, IonContent, IonInput, IonInputPasswordToggle, IonButton, IonSpinner, IonIcon, IonText],
})
export class LoginPage implements OnInit {
  private auth = inject(AuthService);
  private router = inject(Router);

  readonly build = BUILD;
  server = '';
  username = '';
  password = '';
  showServer = signal(false);
  busy = signal(false);
  error = signal<string | null>(null);

  constructor() {
    addIcons({ flash, serverOutline });
  }

  async ngOnInit(): Promise<void> {
    this.server = await this.auth.lastServer();
  }

  async submit(): Promise<void> {
    if (this.busy() || !this.username || !this.password) return;
    this.busy.set(true);
    this.error.set(null);
    try {
      await this.auth.login(this.server, this.username.trim(), this.password);
      this.password = '';
      await this.router.navigateByUrl('/tabs/home', { replaceUrl: true });
    } catch (e: any) {
      this.error.set(
        e?.status === 401 ? 'Wrong username or password'
        : e?.status === 429 ? 'Too many attempts. Wait a few minutes and try again.'
        : e?.status === 0 ? `Can't reach ${this.server}. Are you on the home network?`
        : e?.error?.detail || 'Login failed');
      if (e?.status === 0) this.showServer.set(true);
    } finally {
      this.busy.set(false);
    }
  }
}
