# Home app (Ionic Angular + Capacitor)

iOS/Android app for the home dashboard. Talks to the server's `/api/v1` (see the main README).

**Screens:** Login → tabs **Home** (power now, today's kWh/cost, heater with boost, laundry, cameras with live video, activity) ·
**Energy** (24 h / 7 d / 30 d / 12 months, kWh or €, stacked chart per device, breakdown, full table) ·
**Devices** (lights with brightness, plugs, heater, H100 buttons; protected plugs ask before switching off).

## Develop

```bash
cd app
npm install
npx ionic serve            # http://localhost:8100, sign in with the dashboard user
```
The login screen is pre-filled with `http://192.168.13.173:8811` (`src/environments/`); tap the address under the button to change it.

## Run on a phone

```bash
npx ionic build && npx cap sync
npx cap open ios           # Xcode: pick your team under Signing, run on the iPhone
npx cap open android       # Android Studio: run on the phone (needs JDK 21, Studio's bundled one works)
```

## How it talks to the server

- Login gets a 1 h access token + 90-day refresh token, stored with Capacitor Preferences. Every API call
  sends the Bearer token; a 401 triggers one refresh and a retry; if that fails, back to the login screen.
- Camera images, live video and the WebSocket use a 15-min stream token in the URL.
- Live updates come over the WebSocket; it disconnects when the app goes to the background and resyncs on resume.
- The server is plain HTTP on the LAN, so the native projects allow it: Android serves the app from
  `http://localhost` with cleartext traffic on; iOS has `NSAllowsArbitraryLoads` + a local-network usage text.
  Outside the home network you need a VPN (e.g. Tailscale) to reach the server.
