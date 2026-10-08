"""Print every Tapo device on the LAN, its children, features and S200B trigger logs.

Usage: python discover.py            (uses TAPO_USER / TAPO_PASS / TAPO_HOSTS from .env)
"""
import asyncio
import os

from dotenv import load_dotenv
from kasa import Credentials, Discover

load_dotenv()


def dump(dev, indent=""):
    print(f"{indent}{dev.alias!r} {dev.model} type={getattr(dev.device_type, 'value', dev.device_type)} id={dev.device_id}")
    for fid, f in dev.features.items():
        try:
            print(f"{indent}   · {fid} [{f.type.name}] = {f.value!r}")
        except Exception as e:  # noqa: BLE001
            print(f"{indent}   · {fid} [error: {e}]")
    mod = dev.modules.get("TriggerLogs")
    if mod is not None:
        print(f"{indent}   trigger logs: {getattr(mod, 'logs', None)}")


async def main():
    creds = Credentials(os.environ["TAPO_USER"], os.environ["TAPO_PASS"])
    hosts = [h.strip() for h in os.getenv("TAPO_HOSTS", "").split(",") if h.strip()]
    if hosts:
        devs = {h: await Discover.discover_single(h, credentials=creds) for h in hosts}
    else:
        devs = await Discover.discover(credentials=creds)
    if not devs:
        print("Nothing found. Try setting TAPO_HOSTS.")
    for ip, dev in devs.items():
        print(f"\n=== {ip}")
        try:
            await dev.update()
        except Exception as e:  # noqa: BLE001
            print(f"  update failed: {e}")
            continue
        dump(dev, "  ")
        for child in dev.children:
            dump(child, "    └ ")
        await dev.disconnect()


asyncio.run(main())
