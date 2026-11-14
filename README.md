# autoTV

Turn the living room TV on from a Raspberry Pi and play fish or whale ambiance
videos on YouTube, on a schedule.

The Pi talks to the Roku over the home network using Roku's built-in External
Control Protocol. Nothing is plugged into the TV, and there is nothing to
install beyond Python 3.11+, which Raspberry Pi OS already has.

## Set up

**On the Roku**

1. Settings > System > Advanced system settings > Control by mobile apps >
   Network access: **Default** (or Permissive).
2. Roku TV only: Settings > System > Power > **Fast TV start** on, so the TV
   stays reachable while it is off.
3. Roku player (stick or box) only: turn on HDMI-CEC on the TV, and on the Roku
   Settings > System > Control other devices > **1-touch play**. That is how the
   player switches the TV on.
4. Make sure the YouTube app is installed.

**On the Pi** (same network as the Roku)

```bash
git clone https://github.com/dancey-apple/autoTV.git
```

```bash
cd autoTV
```

```bash
./autotv.py status
```

`status` finds the Roku and shows its name and power state. If you have more
than one Roku, run `./autotv.py discover` and put the right address in
`config.toml` (copy it from `config.example.toml`).

## Add videos

Find long ambiance videos on YouTube and paste their links:

```bash
./autotv.py add fish https://www.youtube.com/watch?v=XXXXXXXXXXX
```

```bash
./autotv.py add whale https://youtu.be/XXXXXXXXXXX
```

They are saved in `videos.txt`, which you can also edit by hand.
`./autotv.py list` shows them.

## Use

```bash
./autotv.py on          # TV on, random video
./autotv.py on whale    # only whale videos
./autotv.py off
```

`on` picks a random video, never the same one twice in a row. If someone is
already watching something, it leaves the TV alone; `--force` overrides that.

## Schedule

```bash
./install.sh 08:00 22:30
```

Plays every day at 08:00 and turns the TV off at 22:30. The off time is
optional. `./install.sh --remove` removes the schedule, and
`journalctl --user -u autotv-on` shows what the last run did.

## Troubleshooting

- **"the Roku refused ..."**: step 1 above.
- **TV does not wake**: check Fast TV start, and give the Roku a fixed IP
  address in your router. Wake-on-LAN is tried automatically once the Roku has
  been seen once.
- **YouTube stops at "Who's watching?"**: set `post_launch_keys = ["Select"]`
  in `config.toml`.
- **A Roku player cannot turn the TV off**: `off` only stops playback; the TV's
  own sleep timer has to do the rest.

## Tests

```bash
python3 -m unittest
```

The tests run against a fake Roku, so they need no hardware.
