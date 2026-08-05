# MIDI → tiny browser chiptunes

This converter turns Standard MIDI files into compact `.chip` event files and
plays them with the browser's Web Audio API. It does **not** render sampled
audio, so the files are typically much smaller than MP3, Ogg, or WAV.

The `.chip` format is custom to the included player. Keep `chip-player.js` in
your site; regular audio players will not recognize these files.

## Convert MIDI files

Python 3.9 or newer is enough; there are no packages to install.

```sh
python3 midi_to_chip.py song1.mid song2.mid song3.mid --output-dir web/music
```

This creates one `.chip` file per input plus `web/music/manifest.json`. The
defaults use four simultaneous voices, 5 ms timing precision,
square/triangle/saw waves, and noise for MIDI channel 10 percussion.

Preview the included page from this directory:

```sh
python3 -m http.server 8000
```

Then visit <http://localhost:8000/web/>. Browsers block Web Audio until a user
clicks, which the example's Play buttons handle.

## Add it to an existing website

```html
<button id="play">Play</button>
<button id="stop">Stop</button>
<script type="module">
  import { ChipPlayer } from "/music/chip-player.js";

  const player = new ChipPlayer();
  const song = await player.load("/music/song1.chip");
  document.querySelector("#play").onclick = () => player.play(song);
  document.querySelector("#stop").onclick = () => player.stop();
</script>
```

The player defaults to 60% output and includes a dynamics limiter so dense
four-voice passages can play loudly without hard clipping. Use
`player.setVolume(0.8)` at any time for a different level; values are clamped
to the range `0`–`1`.

Serve the files over HTTP(S), rather than opening the HTML with a `file://` URL.
Your web server can send `.chip` files as `application/octet-stream`.

## Useful controls

```text
--max-polyphony 3        More authentic/smaller, but drops more chord notes
--quantum-ms 10          Smaller timing data, with slightly less precision
--transpose -12          Shift everything down one octave
--minimum-velocity 20    Remove very quiet notes
--waveform square        Force one timbre (auto is the default)
--no-manifest            Do not create manifest.json
```

Run `python3 midi_to_chip.py --help` for the complete CLI. The parser supports
Standard MIDI File types 0 and 1 with ticks-per-quarter-note timing. Type 2 and
SMPTE-timed MIDI files are rejected with a clear error.

## Size knobs

Start with the defaults. If an arrangement sounds too busy, try
`--max-polyphony 3`. If file size matters more than timing nuance, try
`--quantum-ms 10 --minimum-velocity 16`. Always listen after reducing voices:
the converter favors louder overlapping notes, but it cannot know which inner
part is musically essential.

## Run the tests

```sh
python3 -m unittest discover -s tests -v
node --test tests/test_chip_player.mjs
```
