# Changelog

All notable changes to this project are documented here.
This project follows [semantic versioning](https://semver.org/).

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
