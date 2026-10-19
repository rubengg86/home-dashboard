import type { CapacitorConfig } from '@capacitor/cli';

const config: CapacitorConfig = {
  appId: 'com.rubengg86.homedashboard',
  appName: 'Home',
  webDir: 'www',
  server: {
    // the dashboard is plain HTTP on the LAN: serve the app from http://localhost on Android too,
    // otherwise the WebView blocks http:// API calls, camera images and the WebSocket as mixed content
    androidScheme: 'http',
  },
};

export default config;
