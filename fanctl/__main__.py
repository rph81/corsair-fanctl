"""Daemon entry point."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

from . import __version__, arcconf, backends, config as config_module, sensors
from .controller import Controller
from .httpd import make_server, serve_forever

DEFAULT_CONFIG_PATH = "/etc/corsair-fanctl/config.json"
LOG = logging.getLogger("fanctl")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


CONFIG_PATH_HINT = [DEFAULT_CONFIG_PATH]


def _list_sensors(preference: str) -> int:
    """Print every sensor id usable in a fan curve, then exit."""
    host = sensors.HostSensors()
    host.scan()
    values = host.read_all()

    device_entries = []
    try:
        backend = backends.create(preference)
    except Exception as exc:
        print(f"device: unavailable ({exc})\n")
    else:
        description = backend.describe()
        reading = backend.read()
        print(f"device: {description['device']} via {description['backend']} backend")
        for fan in description["fans"]:
            state = fan["type"] or "not connected"
            rpm = reading["rpm"].get(fan["index"])
            suffix = f" - {rpm} rpm" if rpm is not None else ""
            print(f"  fan{fan['index']}: {state}{suffix}")
        for entry in sensors.device_catalog(description):
            probe = int(entry["id"].rsplit("temp", 1)[1])
            values[entry["id"]] = reading["temps"].get(probe)
        device_entries = sensors.device_catalog(description)
        backend.close()
        print()

    storage_entries: list = []
    try:
        storage_cfg = config_module.load(CONFIG_PATH_HINT[0])["storage"]
    except (RuntimeError, KeyError):
        storage_cfg = None
    if storage_cfg and storage_cfg.get("enabled"):
        poller = arcconf.ArcconfSensors(storage_cfg)
        poller._poll_once()
        status = poller.status()
        if status["error"]:
            print(f"storage: unavailable ({status['error']})\n")
        else:
            print(f"storage: {len(status['drives'])} drive(s) via arcconf")
            for drive in status["drives"]:
                temp = drive["temperature"]
                where = f"slot {drive['slot']}" if drive["slot"] is not None else "?"
                reading = f"{temp:.0f} C" if temp is not None else "n/a"
                print(f"  {where}: {drive['path'] or '?':<10} {reading}")
            print()
        storage_entries = poller.catalog()
        for entry in storage_entries:
            for drive in status["drives"]:
                if drive["id"] == entry["id"] and drive["temperature"] is not None:
                    values[entry["id"]] = drive["temperature"]
            if entry["id"].endswith(":max"):
                temps = [d["temperature"] for d in status["drives"]
                         if d["temperature"] is not None]
                if temps:
                    values[entry["id"]] = max(temps)

    print("sensor ids:")
    for entry in device_entries + host.catalog() + storage_entries:
        value = values.get(entry["id"])
        reading_text = f"{value:.1f} C" if isinstance(value, (int, float)) else "n/a"
        print(f"  {entry['id']:<34} {reading_text:>8}  {entry['label']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="corsair-fanctl",
        description="Fan curve control and web UI for the Corsair Commander Pro.",
    )
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG_PATH,
                        help=f"config file path (default: {DEFAULT_CONFIG_PATH})")
    parser.add_argument("--bind", help="override the listen address from the config")
    parser.add_argument("--port", type=int, help="override the listen port from the config")
    parser.add_argument("--log-level", default="info",
                        choices=["debug", "info", "warning", "error"])
    parser.add_argument("--list-sensors", action="store_true",
                        help="print available sensor ids and exit")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    _setup_logging(args.log_level)

    if args.list_sensors:
        CONFIG_PATH_HINT[0] = args.config
        preference = "auto"
        try:
            preference = config_module.load(args.config)["control"]["backend"]
        except RuntimeError:
            pass
        return _list_sensors(preference)

    controller = Controller(args.config)
    # Persist immediately so a fresh install lands a complete, editable file.
    config_module.save(args.config, controller.config)

    bind = args.bind or controller.config["http"]["bind"]
    port = args.port or controller.config["http"]["port"]
    token = controller.config["http"]["auth_token"]

    try:
        server = make_server(controller, bind, port, token)
    except OSError as exc:
        LOG.error("cannot bind %s:%s: %s", bind, port, exc)
        return 1

    controller.start()
    serve_forever(server)
    LOG.info("listening on http://%s:%s/ (auth %s)",
             bind, port, "enabled" if token else "disabled")

    done = threading.Event()

    def _handle_signal(signum, _frame):
        LOG.info("received %s, shutting down", signal.Signals(signum).name)
        done.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        done.wait()
    finally:
        server.shutdown()
        server.server_close()
        controller.stop()
    LOG.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
