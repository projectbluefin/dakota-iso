# Utah gnome-setup medium (experimental)

The raw live medium that boots Utah and installs it by block copy with GNOME
STF's gnome-setup driving systemd-sysinstall. Moved from `projectbluefin/utah`;
the design is its ADR-0001, and the shared design with Dakota is under
discussion in #413.

- Paths below this directory mirror the Utah repository (`iso/raw/`,
  `iso/scripts/`, `scripts/`), and the files are byte-identical to
  `hanthor/utah` `feat/shared-gnome-setup`, so the move can be checked with
  `diff -r`. `compare_paths.py`, which reads Utah's own CI, stayed behind.
- gnome-setup is built from the `bluefin` branch of `hanthor/gnome-setup`, the
  one meant for Dakota too.
- `iso/scripts/luks-unlock.py` duplicates `live/src/luks-unlock.py` with a
  different monitor transport. Merge them after the cutover, when both lanes
  can be tested together.
- `.github/workflows/utah-image-installer.yml` builds it and runs the GUI E2E on
  pull requests that touch it and on dispatch. It publishes nothing.

## Cutover

Until all of these hold, `projectbluefin/utah` keeps its copy, its nightly run
and the publishing of `ghcr.io/projectbluefin/utah-installer`:

1. The E2E passes here.
2. The package `utah-installer` grants this repository write access, and
   publishing moves here.
3. Utah drops `iso/raw/`, `image-installer.yml` and `publish-installer.yml` in
   the same change window, so there is one copy again.

`dakota/` beside this directory is left for #394 or whatever #413 decides.
