# Barry Benchmark Tools

> [!IMPORTANT]
> **This repo was built with a coding agent: [Claude Code](https://www.anthropic.com/claude-code),
> running Anthropic's Claude Opus 5.5 (`claude-opus-5-5`).** Claude wrote the
> code, the commit messages and this README. People set the goals, made the
> decisions and did the hands-on testing. Review the code before you rely on
> it. See [a note from lavachemist](https://github.com/project-barry), a human, on Project Barry and generative AI.

> [!TIP]
> **Join the Project Barry community on Discord:** https://discord.gg/euPurKCWc4
>
> **Watch Project Barry on YouTube:** https://www.youtube.com/@Project-Barry

A headless benchmark harness for SteamOS on ARM handhelds (developed on a
Retroid Pocket 6, Snapdragon 8 Gen 2 / SM8550, Adreno 740 with Turnip). It
measures what CPU, GPU and memory changes (clock caps, governors, scheduler,
zram, ...) do to real workloads, with no one holding the device:

- native Vulkan (vkmark, offscreen)
- Windows games through Proton (DXVK, VKD3D-Proton), launched into the
  running Gaming Mode session over SSH
- the same game under different Proton versions (Valve's, or custom ones in
  `compatibilitytools.d` such as GE-Proton)
- the same game on the device's stock Android (over adb, no root), to compare
  SteamOS with Android on the same hardware (see [Android devices](#android-devices))

Every run records whether x86 code was emulated, and how (FEX through Wine
WoW64 / ARM64EC, or FEX for the whole process).

## Quick start

The harness runs on the device, but you drive it from your own machine and
the results are saved there.

1. On your machine, in this repo, register the device (an IP address or host
   name on your LAN or tailnet; the user is the one running Steam in Gaming
   Mode, reachable with your SSH key):

   ```sh
   ./bench devices add rp6 steamos@192.0.2.10 --name "Retroid Pocket 6"
   ./bench devices add thor steamos@thor.example.ts.net --name "AYN Thor"
   ./bench devices                  # list; * marks the default
   ./bench devices default rp6      # the one used without --device
   ./bench devices test thor        # can we log in, what does it offer?
   ```

   The list lives in `devices.json` (git-ignored). Pick a device per command
   with `./bench --device thor ...`. Devices can also be added, tested and
   removed in the web app.
2. `./bench setup`: fetches vkmark into `~/bench/opt` on the device (no root).
3. Run, change something, run again, compare:

```sh
./bench run matrices/first-light.yaml --tag baseline
# ... change something on the device (clock cap, governor, ...) ...
./bench run matrices/first-light.yaml --tag gpu-cap-550
./bench compare baseline gpu-cap-550
```

`bench run` copies the harness to `~/bench` on the device, uploads the
matrix, and starts the session there as a systemd user unit. It then streams
the log, and when the session ends it copies the session folder to
`./results/`. The device copy is deleted only after the local copy has the
same number of files and bytes. The session keeps going if SSH drops or you
press Ctrl-C:

| Command                | What it does                                                       |
| ---------------------- | ------------------------------------------------------------------ |
| `bench status`         | what is running on the device, what has not been pulled yet        |
| `bench attach`         | follow the current (or last) run, then pull it                     |
| `bench stop`           | end the running session (it writes what it measured), then pull it |
| `bench pull`           | fetch finished sessions still on the device                        |
| `bench deploy`         | copy the harness to the device without running anything           |
| `bench list`           | local sessions                                                     |
| `bench compare A B`    | local sessions, by tag or folder name                              |
| `bench web`            | the web app (below)                                                |

Add `--keep-remote` to keep the device copy. Each session's folder name ends
in its device id (`20260930-1300_baseline_thor`), so sessions from several
devices sit side by side. `bench compare baseline@rp6 baseline@thor` compares
two devices; a plain tag picks the newest session with that tag. Every other command (`snapshot`,
`sample 30`, `steam tools`, `steam status <appid>`, `steam set-tool <appid>
<tool>`, `steam unwrap <appid>`) runs on the device and prints here.

With no devices registered, `bench` works locally on the device itself, with
results in `~/bench/results`. (A `remote.conf` from an earlier version is
imported into `devices.json` the first time.)

## Web app

```sh
./bench web                      # opens http://localhost:8765 in your browser
./bench web --host 0.0.0.0       # also reachable from other devices on your network
```

It's a browser front end for the same harness. It works in current Chrome,
Edge, Firefox and Safari on Windows, macOS, Linux and ChromeOS, including
phones and tablets.

- **Sessions**: every saved session, with its headline numbers and flag count.
  Tick two to compare them.
- **Session**: stat tiles, flags, a per-run chart and table for each scenario,
  the Markdown reports (rendered, or as plain text) and downloads. Open a run
  to see its frame-time chart (the trimmed parts are shaded) and its CPU/GPU
  clocks, temperatures, power and load over time.
- **Compare**: the % change per metric, marked better / worse / within noise,
  plus the system settings that differ between the two sessions.
- **New run**: pick a matrix, set a tag, check the scenarios on the device,
  pick one or more devices, start the run, and watch its output live. The run continues
  on the device if you close the page; the results come back here when it
  ends.
- **Devices**: add a device by IP address or host name, test the connection
  (it reports the model, OS, Python/PyYAML, whether Steam runs, and what is
  installed), set the default, edit or remove it. Each device's page shows
  what is running there, results not copied here yet, current clocks and
  settings, live sensors, and buttons for fetch / stop / install or update
  the harness / install vkmark.
- **New run** can start the same matrix on several devices at once (A/B
  between devices): each writes its own session with the same tag, ready for
  Compare.
- **Matrices**: edit, create and check matrix files in `matrices/`.

The server uses only Python's standard library and loads nothing from the
internet. It runs on the machine that drives the device (macOS or Linux),
while the browser can be anywhere. Runs, pulls and so on are ordinary
`bench` commands started by the server, so the web app and the CLI always
agree.

Access: the server prints a link with a token. Opening it signs that browser
in (the cookie is HttpOnly and SameSite=Lax, so links opened from chat apps work), and every request needs it.
Changes also need a custom header that other websites cannot send. By default
it listens on 127.0.0.1 only. With `--host 0.0.0.0`, anyone on your network
who has the link can start runs, so only share the link with people you
trust. The token is kept in `.bbt-web/token` (git-ignored); delete that file
to issue a new one.

## What a run does

For each scenario: one discarded warm-up run, then N measured runs (3 by
default). Before every run the harness waits until CPU and GPU temperatures
are back within a tolerance of the idle baseline measured at session start.

Steam scenarios:

1. Map the Proton version (`CompatToolMapping` in `config.vdf`) and set the
   launch options to `~/bench/bin/bbt-wrap %command%`. Steam is stopped for
   the edit (`systemctl --user stop steam.service`), a backup is written to
   `~/bench/state/backups/`, and Steam is started again. Nothing is edited if
   the settings already match.
2. Optionally write game settings into the Proton prefix's registry (the
   prefix must not be in use; backed up first).
3. Launch with `steam -ifrunning steam://rungameid/<appid>`. The wrapper adds
   MangoHud logging and the scenario's arguments only while a run is active
   (`~/bench/state/run.env`, which expires), so normal play is unaffected.
4. Capture every frame with MangoHud (an invisible HUD: `no_display` also
   disables logging), plus the harness's own sysfs sampler.
5. Close the game (or let a built-in benchmark quit by itself), collect the
   game's own result files, cool down, repeat.

The harness only reads sysfs. It never writes clock, voltage or power
controls: make those changes yourself between sessions, and the snapshot
records them.

## Android devices

An Android device is driven over adb instead of SSH. Nothing is installed on
it and no root is needed; the session runs on your machine and the results
land in the same `results/` folder, so `bench compare` works across the two
systems.

```sh
# on the device: Developer options > Wireless debugging > Pair device with pairing code
adb pair IP:PAIRPORT && adb connect IP:PORT
./bench devices add rp6-android adb:$(adb shell getprop ro.serialno) --name "Retroid Pocket 6 (Android)"
./bench --device rp6-android run matrices/android-tr2013.yaml --tag android-stock
./bench compare first-light@rp6 android-stock@rp6-android
```

The device is found by its serial, so a new wireless-debugging port does not
matter (it reconnects through mDNS when it can). `snapshot` and `sample` work
too; `deploy`, `attach`, `stop` and `pull` do not apply (stop a run with
Ctrl-C: it keeps what it measured).

What a run does (`kind: android`, see `bbt/android.py`):

1. Force-stop the app, start Perfetto's SurfaceFlinger frame timeline and a
   small sh sampler in `/data/local/tmp/bbt` (same columns as on SteamOS).
2. Start the game with `am start ...` (`launch:`), or ask you to start it
   (`launch: manual`, with a notification on the device).
3. Capture until the game quits (`capture: until_exit`) or for a window. The
   game is running while `process:` matches a `ps` command line (or a layer
   matches `layer:`, or the package is alive).
4. Stop the trace, force-stop the app, pull the trace. Frame times are the
   gaps between frames reaching the screen, taken from the busiest layer of
   the app (or the one matching `layer:`); the trace is decoded here with the
   standard library.

Differences from SteamOS worth keeping in mind: Android does not show frames
faster than the display refreshes (put the display at 120 Hz), GPU busy is
the whole GPU (KGSL) rather than the game's processes, and the game's own
result files stay inside the app. Scenarios whose `title` matches pair up in
`bench compare` even when their names differ.

## Matrix file

```yaml
session:
  runs: 3
  warmup: 1
  variance_cv_pct: 3          # flag a scenario when avg FPS / score varies more
  cooldown: {tolerance_c: 3, min_s: 20, max_s: 600}

scenarios:
  - name: vkmark-1080p
    title: vkmark             # groups scenarios into one report
    kind: vkmark
    vkmark: {winsys: headless, size: 1920x1080, benchmarks: [vertex:duration=10]}

  - name: some-game-dx12-ge
    title: Some Game
    kind: steam
    appid: 123456
    proton: GE-Proton10-20    # a name from `bench steam tools`
    args: [-benchmark]
    env: {DXVK_ASYNC: "1"}
    capture: window           # or until_exit for benchmarks that quit
    settle_s: 45              # window: first frame -> capture start
    duration_s: 60
```

`until_exit` scenarios take `trim_start_s` / `trim_end_s` to cut loading and
fade-out from the log. `wine_registry` and `game_results` are shown in
`matrices/first-light.yaml` (Tomb Raider's built-in benchmark).
`matrices/tr2013-high.yaml` is the same with 2x SSAA and TressFX: on faster
devices (the KONKR Pocket FIT) first-light's Tomb Raider sits at the 120 Hz
cap, where CPU and GPU changes don't show.

## Results

`results/<YYYYMMDD-HHMM>_<tag>/` (on your machine in remote mode):

| File                               | Content                                                     |
| ---------------------------------- | ----------------------------------------------------------- |
| `bbt_<title>_<YYYYMMDDHHMM>.md`    | Report per title: test conditions, results, per-run, flags  |
| `runs.csv`                         | One row per run (warm-ups marked), every metric             |
| `summary.json`                     | Per scenario: runs, mean/median/stdev/CV, flags             |
| `session.json`                     | Tag, matrix, system snapshot and baseline temps at start    |
| `<scenario>/<run>/`                | `snapshot.json`, `samples.csv`, MangoHud log, game files    |
|                                    | (Android: `frames.csv`, `frametimeline.pftrace`)            |
| `remote-run.log`                   | The session's console output (remote mode)                  |

Metrics: average FPS, 1% and 0.1% lows (1000 / mean of the slowest 1% /
0.1% frame times), frame-time p50/p90/p95/p99/p99.9 and stdev, hitches,
per-cluster CPU load and clocks, GPU clock and busy % (DRM fdinfo), CPU /
GPU / memory temperatures, throttling states, system power, energy per frame,
RAM / swap / GPU memory.

Power: on battery, battery voltage x current (this PMIC's `power_now` is not
usable). On a charger, USB input minus the battery's charge power, marked as
an estimate. The report states which one applies.

Flags: fewer than 3 good runs, high variance, outlier runs, runs started
before temperatures recovered, thermal throttling, power source changes, and
frame rates sitting on a refresh-rate cap.

## Requirements

On the device: Python 3.10+ with PyYAML (both on SteamOS), `vulkaninfo`,
MangoHud, and Steam in Gaming Mode as a systemd user unit (`steam.service`).
On your machine: Python 3.12+ and ssh (no PyYAML needed). For Android
devices also adb (`brew install --cask android-platform-tools`) and PyYAML,
since those sessions run on your machine.

## How this was made

Written with Claude Code (Anthropic) under the direction of lavachemist, and
tested on the device as it was built.
