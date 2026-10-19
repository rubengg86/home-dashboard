import { Routes } from '@angular/router';
import { TabsPage } from './tabs.page';

export const routes: Routes = [
  {
    path: 'tabs',
    component: TabsPage,
    children: [
      { path: 'home', loadComponent: () => import('../home/home.page').then((m) => m.HomePage) },
      { path: 'energy', loadComponent: () => import('../energy/energy.page').then((m) => m.EnergyPage) },
      { path: 'devices', loadComponent: () => import('../devices/devices.page').then((m) => m.DevicesPage) },
      { path: '', redirectTo: '/tabs/home', pathMatch: 'full' },
    ],
  },
  { path: '', redirectTo: '/tabs/home', pathMatch: 'full' },
];
