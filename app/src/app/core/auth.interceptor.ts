import { HttpErrorResponse, HttpInterceptorFn, HttpRequest } from '@angular/common/http';
import { inject } from '@angular/core';
import { Router } from '@angular/router';
import { catchError, from, switchMap, throwError } from 'rxjs';
import { AuthService } from './auth.service';

/** Prefixes relative /api/... URLs with the server, adds the Bearer token, and on a 401 refreshes once and retries. */
export const authInterceptor: HttpInterceptorFn = (req, next) => {
  const auth = inject(AuthService);
  const router = inject(Router);
  if (!req.url.startsWith('/api/')) return next(req);

  const send = (token: string | null) => {
    const r: HttpRequest<unknown> = req.clone({
      url: auth.server() + req.url,
      setHeaders: token ? { Authorization: `Bearer ${token}` } : {},
    });
    return next(r);
  };

  return from(auth.freshAccessToken()).pipe(
    switchMap((token) => send(token)),
    catchError((err: unknown) => {
      if (!(err instanceof HttpErrorResponse) || err.status !== 401) return throwError(() => err);
      return from(auth.refresh()).pipe(switchMap((ok) => {
        if (!ok) {
          router.navigateByUrl('/login', { replaceUrl: true });
          return throwError(() => err);
        }
        return send(auth.accessToken());
      }));
    }),
  );
};
