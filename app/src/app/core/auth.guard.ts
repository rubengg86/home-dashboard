import { inject } from '@angular/core';
import { CanActivateFn, Router } from '@angular/router';
import { AuthService } from './auth.service';

// inject() before the first await: the injection context ends there
export const authGuard: CanActivateFn = async () => {
  const auth = inject(AuthService), router = inject(Router);
  await auth.whenReady();
  return auth.loggedIn() ? true : router.createUrlTree(['/login']);
};

export const guestGuard: CanActivateFn = async () => {
  const auth = inject(AuthService), router = inject(Router);
  await auth.whenReady();
  return auth.loggedIn() ? router.createUrlTree(['/tabs/home']) : true;
};
