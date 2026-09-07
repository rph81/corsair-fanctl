# Contributing

Bug reports and patches are welcome. This is a small project with a narrow
scope — fan control for the Corsair Commander Pro — so please read the scope
notes below before starting significant work.

## Reporting a problem

Open an issue and include:

- the output of `curl -s localhost:8899/api/state | python3 -m json.tool | head -60`
- `journalctl -u corsair-fanctl -n 50 --no-pager`
- your Proxmox / kernel version (`pveversion`, `uname -r`)

Redact drive serial numbers if you paste `arcconf` output.

## Running the tests

No hardware required. The simulator builds a fake sysfs tree and a stand-in for
`arcconf`, with a thermal model where temperatures respond to the duties the
control loop writes:

```bash
python3 tools/selftest.py          # curve, config, arcconf parser, control loop
python3 tools/devsim.py --port 8899 # then open http://127.0.0.1:8899/
```

Please add a test for any behaviour change to the control loop, the config
normaliser, or the `arcconf` parser. `tools/fixtures/` holds captured controller
output — contributions of output from other Adaptec/Microsemi models are
genuinely useful, with serial numbers replaced.

## Scope

In scope: fan control, temperature sources, the web UI, and packaging.

Out of scope: **RGB and lighting.** The Commander Pro drives LED channels and
liquidctl supports them, but this project deliberately does not. Keeping lighting
out is what keeps the daemon dependency-free and the UI focused.

Support for other storage controllers (`storcli`, `perccli`, plain `smartctl`)
would be welcome — each needs its own parser alongside `fanctl/arcconf.py`,
following the same shape.

## Style

- Standard library only in `fanctl/`. `liquidctl` is an optional backend and
  must stay optional.
- No build step for the web UI: plain HTML, CSS and JavaScript.
- Comment the *why*, not the *what*, and especially the hardware quirks — the
  non-obvious constraints are the valuable part of this codebase.
