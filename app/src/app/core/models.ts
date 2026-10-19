// Shapes returned by the dashboard API (/api/v1). Kept loose where the server passes device data through.

export interface Tokens {
  token_type: 'Bearer';
  access_token: string;
  expires_in: number;
  refresh_token: string;
  refresh_expires_in: number;
  session_id: string;
}

export interface Sensor {
  id: string;
  name: string;
  value: string | number | boolean | null;
  unit: string | null;
}

export interface Device {
  id: string;
  alias: string;
  model: string;
  type: string; // bulb | plug | hub | sensor | ...
  host: string;
  online: boolean;
  error: string | null;
  switchable: boolean;
  on: boolean | null;
  confirm_off: boolean;
  dimmable: boolean;
  brightness: number | null;
  sensors: Sensor[];
  children?: Device[];
}

export interface Heater {
  id: string;
  name: string;
  model: string;
  ip: string | null;
  online: boolean;
  error: string | null;
  on: boolean;
  status: 'Offline' | 'Off' | 'On' | 'Heating' | 'Hot';
  power_w: number | null;
  current_a: number | null;
  voltage_v: number | null;
  boost_ends_at: string | null;
  on_since: string | null;
  kwh_today: number;
  kwh_7d: number;
  cost_today: number | null;
  cost_7d: number | null;
  currency: string;
  faults: string[];
  has_boost: boolean;
  max_on_minutes: number | null;
  energy_source: string;
  energy_error: string | null;
}

export interface Appliance {
  id: string;
  name: string;
  kind: string; // washer | dryer
  online: boolean;
  power: string | null;
  machine_state: string | null; // run | pause | stop
  status: 'Running' | 'Paused' | 'Off' | 'Idle';
  phase: string | null;
  remaining_min: number | null;
  ends_at: string | null;
  progress: number | null;
  power_w: number | null;
}

export interface Appliances {
  error: string | null;
  auth?: { mode: string; connected: boolean; error: string | null };
  appliances: Appliance[];
}

export interface DashEvent {
  device_id: string;
  alias: string;
  event: string;
  source?: string;
  timestamp: string;
  received: string;
}

export interface Camera {
  id: string;
  name: string;
  host: string;
  online: boolean;
  error: string | null;
  recordings: boolean;
}

export interface Cameras {
  refresh_seconds: number;
  live: boolean;
  cameras: Camera[];
}

export interface Overview {
  devices: Device[];
  heaters: Heater[];
  appliances: Appliances;
  events: DashEvent[];
  cameras: Cameras;
  price_per_kwh: number;
  currency: string;
}

export type EnergyRange = '24h' | '7d' | '30d' | '12m';

export interface EnergySeries {
  id: string;
  name: string;
  kind: 'plug' | 'heater';
  power_w: number | null;
  since: string | null;
}

export interface Energy {
  price: number;
  currency: string;
  series: EnergySeries[];
  ranges: Record<EnergyRange, { buckets: string[]; values: Record<string, (number | null)[]> }>;
}
