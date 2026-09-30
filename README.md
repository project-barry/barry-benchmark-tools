# Barry Benchmark Tools

A headless benchmark harness for SteamOS on ARM handhelds (developed on a
Retroid Pocket 6, Snapdragon 8 Gen 2 / SM8550, Adreno 740 with Turnip). It
measures what CPU, GPU and memory changes (clock caps, governors, scheduler,
zram, ...) do to real workloads, with no one holding the device:

- native Vulkan (vkmark, offscreen)
- Windows games through Proton (DXVK, VKD3D-Proton), launched into the
  running Gaming Mode session over SSH
- the same game under different Proton versions (Valve's, or custom ones in
  `compatibilitytools.d` such as GE-Proton)

Every run records whether x86 code was emulated, and how (FEX through Wine
WoW64 / ARM64EC, or FEX for the whole process).

## Quick start

The harness runs on the device, but you drive it from your own machine and
the results are saved there.

1. On your machine, in this repo: `cp remote.conf.example remote.conf` and set
   `target=steamos@<device>`. The user must be the one running Steam in Gaming
   Mode, reachable with an SSH key. `remote.conf` is git-ignored.
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

Add `--keep-remote` to keep the device copy. Every other command (`snapshot`,
`sample 30`, `steam tools`, `steam status <appid>`, `steam set-tool <appid>
<tool>`, `steam unwrap <appid>`) runs on the device and prints here.

Without a `remote.conf`, `bench` works locally on the device itself, with
results in `~/bench/results`.

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

## Results

`results/<YYYYMMDD-HHMM>_<tag>/` (on your machine in remote mode):

| File                               | Content                                                     |
| ---------------------------------- | ----------------------------------------------------------- |
| `bbt_<title>_<YYYYMMDDHHMM>.md`    | Report per title: test conditions, results, per-run, flags  |
| `runs.csv`                         | One row per run (warm-ups marked), every metric             |
| `summary.json`                     | Per scenario: runs, mean/median/stdev/CV, flags             |
| `session.json`                     | Tag, matrix, system snapshot and baseline temps at start    |
| `<scenario>/<run>/`                | `snapshot.json`, `samples.csv`, MangoHud log, game files    |
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
On your machine: Python 3.12+ and ssh (no PyYAML needed).

## How this was made

Written with Claude Code (Anthropic) under the direction of lavachemist, and
tested on the device as it was built.
