# Changelog

All notable changes to this project are documented here.
This project follows [semantic versioning](https://semver.org/).

## [1.4.1]

### Added
- Chart history now survives a service restart. It is saved to
  `/var/lib/corsair-fanctl/history.json` once a minute and on shutdown, and
  restored at startup. `history.persist`, `history.file` (config file only)
  and `history.save_interval` control it.

### Changed
- Default history retention is now 7 hours (was 1 hour), so the chart's
  6-hour range is actually usable. Existing configs keep their own value;
  raise it under **Settings → History retained**.

## [1.4.0]

### Added
- The History chart legend is now a set of toggles: click a series to hide or
  show it, shift-click to see it on its own, and **Show all** to bring
  everything back. The selection is remembered per browser.

## [1.3.0]

### Fixed
- The hwmon backend refused to open when nothing was on fan channel 1: it
  checked only `pwm1`, which the kernel driver does not create for an empty
  channel, so the device never connected and every fan sat at the failsafe
  duty. It now checks whichever `pwmN` attributes exist.
- Emergency detection used the channel's mixed temperature, so with
  `mix: "min"` or `"avg"` one hot drive next to a cool one never tripped it.
  Any single sensor over the emergency temperature now triggers it.
- A config file that could not be parsed made the daemon crash-loop under
  systemd with the fans unmanaged. It is now moved aside as
  `config.json.corrupt-<time>`, the daemon starts on defaults, and the UI
  shows a banner explaining what happened.
- The daemon exited when the web UI port could not be bound. Fan control now
  starts first and the bind is retried every 5 s; on Linux the socket uses
  `IP_FREEBIND` so an address that is not configured yet binds anyway.

### Changed
- `failsafe_duty` is now clamped to 20-100 and `emergency_duty` to 50-100,
  and emergency can never be lower than failsafe. Both are the duties used
  when something has already gone wrong, so 0 was never a safe value.
- `POST` and `PUT` requests must carry `Content-Type: application/json`, and
  requests with an `Origin` from another site are refused with 403. Without
  this, any web page open in a browser on the same LAN could turn the fans
  off through the unauthenticated API. `curl` examples in the README now
  include the header.

## [1.2.0]

### Added
- Dark, light and system themes, selectable in **Settings**.
- Accent colour: eight presets plus a full colour picker, and up to ten saved
  favourites. Changes apply instantly and persist server-side.
- Support for **2-wire DC fans** through a per-channel **Fan type** override.
  These fans have no tachometer wire, so the controller often reports the
  channel as empty and never reports RPM.
- `install.sh --port` now applies to an existing config, not just a new one.

### Changed
- Default port is now **8899** (was 8377). Existing installations keep their
  configured port; an upgrade never moves it.

### Fixed
- Forcing a fan mode was silently discarded when the `corsair-cpro` kernel
  driver was bound: liquidctl only logs a warning in that case. The daemon now
  switches to direct USB access whenever a channel is forced.

## [1.1.0]

### Added
- **Storage panel**: drive temperatures from Adaptec/Microsemi controllers via
  `arcconf`, usable as fan curve sources and keyed on physical slot rather than
  `/dev/sdX`, which is not stable across reboots.
- `arcconf` binary auto-discovery across the usual install directories, since
  systemd gives services a minimal `PATH`.
- Running version reported in `/api/state` and shown in the web UI header.

### Changed
- `PUT /api/config` now **merges** rather than replacing wholesale. A partial
  request previously reset every fan curve to defaults.

### Fixed
- `install.sh` used `systemctl enable --now`, which does not restart an already
  running service — upgrades copied new files but kept running the old code.
- The systemd unit no longer waits on `network-online.target`. Fan control
  should not be gated on networking.

## [1.0.0]

Initial release: six-channel fan curve control for the Corsair Commander Pro,
hwmon and liquidctl backends, software-evaluated curves against any host
temperature sensor, failsafe and emergency handling, and a web UI.
