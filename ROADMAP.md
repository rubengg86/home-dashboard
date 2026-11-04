# Roadmap

Feature ideas to tackle later, roughly in the order they're worth building.

## 1. Push notifications

The foundation for most ideas below: the backend already generates events (washer finished,
button presses, heater `safety_off`) but you only see them inside the app.

- Web Push for the PWA (service worker is already there), APNs via Capacitor for the iOS build.
- Per-event settings: washer done, device offline, temperature/humidity thresholds.

## 2. Fridge watchdog

The Nevera plug's live power is already polled. A fridge compressor has a clear duty cycle:
if the plug reports ~0 W for over an hour, or the plug goes offline, something is wrong
(tripped breaker, failed compressor, unplugged). Notify immediately — this one can save a
fridge full of food.

## 3. Temperature & humidity history

T310 readings are polled constantly and thrown away. Store them like plug energy
(`.tuya_energy.json` pattern) and chart them in the Energy tab: daily min/max, trends.
Humidity alert: >70% sustained = mold risk, time to ventilate.

## 4. Hourly electricity prices (PVPC) + smart boost

The REE/ESIOS API publishes tomorrow's hourly PVPC prices every evening, free.

- Show today's price curve; color the energy charts by cost.
- **Smart heater boost**: instead of "Boost 60", a "heat water in the cheapest hour tonight"
  button that picks the cheapest 60-minute window and sets the plug's own countdown.

## 5. Bring! shopping list

We use [Bring!](https://www.getbring.com) — integrate it through the unofficial API wrapper
[miaucl/bring-api](https://github.com/miaucl/bring-api) (`pip install bring-api`, async,
logs in with the Bring! account email/password, used by Home Assistant's integration).

- Show the list on the Home tab, check items off, add items.
- Credentials in `.env` (`BRING_EMAIL` / `BRING_PASS`); it's an unofficial API, so pin the
  version and degrade gracefully when it breaks.

## 6. Scenes & schedules with a UI

`actions.json` works for an engineer, not for both users.

- User-defined scenes ("movie night", "good night" = everything off except Dormitorio at 10%).
- Scheduler: sunset → Salón pared on; cron-style times with a simple editor in the app.

## 7. MCP server

Expose the existing API as MCP tools (devices, scenes, sensors, heater) so Claude can control
and query the house: "is the heater on?", "turn off everything except the office".

## 8. Presence detection

Ping the phones' IPs (DHCP reservations) from the server. Both away → auto "all off" + notify
on camera motion; first one home → welcome lights. No extra hardware.

## 9. Kiosk mode

A stripped always-on layout for an old tablet on the wall: climate, washer, cameras, scene
buttons.
