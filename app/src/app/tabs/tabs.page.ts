import { Component, OnDestroy, OnInit, inject } from '@angular/core';
import { IonTabs, IonTabBar, IonTabButton, IonIcon, IonLabel } from '@ionic/angular';
import { addIcons } from 'ionicons';
import { bulbOutline, flashOutline, homeOutline } from 'ionicons/icons';
import { LiveService } from '../core/live.service';

@Component({
  selector: 'app-tabs',
  templateUrl: 'tabs.page.html',
  styleUrls: ['tabs.page.scss'],
  imports: [IonTabs, IonTabBar, IonTabButton, IonIcon, IonLabel],
})
export class TabsPage implements OnInit, OnDestroy {
  private live = inject(LiveService);

  constructor() {
    addIcons({ homeOutline, flashOutline, bulbOutline });
  }

  // the tabs only exist while logged in: that's when live data runs
  ngOnInit(): void { this.live.start(); }
  ngOnDestroy(): void { this.live.stop(); }
}
