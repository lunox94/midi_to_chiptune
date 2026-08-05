#!/usr/bin/env python3
"""Convert Standard MIDI Files into tiny, browser-playable .chip files.

The output contains only timed note events.  It is intended for the companion
``web/chip-player.js`` Web Audio player, not for general-purpose media players.
No third-party Python packages are required.
"""

from __future__ import annotations

import argparse
import bisect
import json
import struct
import sys
from collections import defaultdict, deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Sequence


MAGIC = b"CHP1"
WAVEFORMS = {"square": 0, "triangle": 1, "saw": 2, "noise": 3}
WAVEFORM_NAMES = tuple(WAVEFORMS)


class MidiError(ValueError):
    """Raised when an unsupported or malformed MIDI file is encountered."""


@dataclass(frozen=True)
class TickNote:
    start_tick: int
    end_tick: int
    note: int
    velocity: int
    channel: int
    program: int


@dataclass(frozen=True)
class Note:
    start_ms: float
    end_ms: float
    note: int
    velocity: int
    channel: int
    program: int
    waveform: int = 0


@dataclass(frozen=True)
class MidiSong:
    ticks_per_beat: int
    notes: tuple[TickNote, ...]
    tempos: tuple[tuple[int, int, int], ...]  # tick, microseconds/beat, order


@dataclass(frozen=True)
class ChipSong:
    quantum_ms: int
    max_polyphony: int
    notes: tuple[Note, ...]

    @property
    def duration_ms(self) -> int:
        return round(max((note.end_ms for note in self.notes), default=0))


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from(">H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from(">I", data, offset)[0]


def _read_varlen(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for _ in range(4):
        if offset >= len(data):
            raise MidiError("truncated variable-length MIDI value")
        byte = data[offset]
        offset += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, offset
    raise MidiError("MIDI variable-length value exceeds four bytes")


def _encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("varints cannot encode negative values")
    out = bytearray((value & 0x7F,))
    value >>= 7
    while value:
        out.append(0x80 | (value & 0x7F))
        value >>= 7
    out.reverse()
    return bytes(out)


def _parse_track(track: bytes, track_number: int) -> tuple[list[TickNote], list[tuple[int, int, int]]]:
    offset = tick = event_order = 0
    running_status: int | None = None
    programs = [0] * 16
    active: dict[tuple[int, int], deque[tuple[int, int, int]]] = defaultdict(deque)
    notes: list[TickNote] = []
    tempos: list[tuple[int, int, int]] = []

    while offset < len(track):
        delta, offset = _read_varlen(track, offset)
        tick += delta
        if offset >= len(track):
            raise MidiError(f"track {track_number}: event is missing its status byte")

        first = track[offset]
        first_data: int | None = None
        if first & 0x80:
            status = first
            offset += 1
            if status < 0xF0:
                running_status = status
            elif status not in (0xF8, 0xFA, 0xFB, 0xFC, 0xFE):
                running_status = None
        else:
            if running_status is None:
                raise MidiError(f"track {track_number}: data byte without running status")
            status = running_status
            first_data = first
            offset += 1

        if status == 0xFF:
            if offset >= len(track):
                raise MidiError(f"track {track_number}: truncated meta event")
            meta_type = track[offset]
            offset += 1
            length, offset = _read_varlen(track, offset)
            payload = track[offset : offset + length]
            if len(payload) != length:
                raise MidiError(f"track {track_number}: truncated meta event payload")
            offset += length
            if meta_type == 0x51:
                if length != 3:
                    raise MidiError(f"track {track_number}: invalid tempo event")
                tempos.append((tick, int.from_bytes(payload, "big"), event_order))
            event_order += 1
            if meta_type == 0x2F:
                break
            continue

        if status in (0xF0, 0xF7):
            length, offset = _read_varlen(track, offset)
            offset += length
            if offset > len(track):
                raise MidiError(f"track {track_number}: truncated SysEx event")
            event_order += 1
            continue

        if status >= 0xF0:
            system_lengths = {0xF1: 1, 0xF2: 2, 0xF3: 1, 0xF6: 0, 0xF8: 0,
                              0xFA: 0, 0xFB: 0, 0xFC: 0, 0xFE: 0}
            if status not in system_lengths:
                raise MidiError(f"track {track_number}: unsupported status 0x{status:02x}")
            offset += system_lengths[status]
            if offset > len(track):
                raise MidiError(f"track {track_number}: truncated system event")
            event_order += 1
            continue

        kind, channel = status >> 4, status & 0x0F
        data_length = 1 if kind in (0xC, 0xD) else 2
        values = [] if first_data is None else [first_data]
        needed = data_length - len(values)
        if offset + needed > len(track):
            raise MidiError(f"track {track_number}: truncated channel event")
        values.extend(track[offset : offset + needed])
        offset += needed

        if kind == 0xC:
            programs[channel] = values[0]
        elif kind == 0x9 and values[1] > 0:
            active[(channel, values[0])].append((tick, values[1], programs[channel]))
        elif kind == 0x8 or (kind == 0x9 and values[1] == 0):
            queue = active[(channel, values[0])]
            if queue:
                start, velocity, program = queue.popleft()
                notes.append(TickNote(start, max(tick, start + 1), values[0], velocity,
                                      channel, program))
        event_order += 1

    # A few exported scores omit note-offs at end-of-track. Close those notes
    # rather than discarding the entire tail of the song.
    for (channel, pitch), queue in active.items():
        for start, velocity, program in queue:
            notes.append(TickNote(start, max(tick, start + 1), pitch, velocity, channel, program))
    return notes, tempos


def parse_midi(data: bytes) -> MidiSong:
    """Parse the note and tempo information needed from an SMF type 0 or 1 file."""
    if len(data) < 14 or data[:4] != b"MThd":
        raise MidiError("not a Standard MIDI File (missing MThd header)")
    header_length = _u32(data, 4)
    if header_length < 6 or 8 + header_length > len(data):
        raise MidiError("invalid MIDI header length")
    midi_format, track_count, division = struct.unpack_from(">HHH", data, 8)
    if midi_format not in (0, 1):
        raise MidiError(f"MIDI format {midi_format} is not supported (use type 0 or 1)")
    if division & 0x8000:
        raise MidiError("SMPTE time division is not supported; use ticks per quarter note")
    if division == 0:
        raise MidiError("MIDI ticks per quarter note cannot be zero")

    offset = 8 + header_length
    all_notes: list[TickNote] = []
    all_tempos: list[tuple[int, int, int]] = []
    for track_number in range(track_count):
        if offset + 8 > len(data) or data[offset : offset + 4] != b"MTrk":
            raise MidiError(f"missing MTrk chunk for track {track_number}")
        length = _u32(data, offset + 4)
        offset += 8
        track = data[offset : offset + length]
        if len(track) != length:
            raise MidiError(f"track {track_number} is truncated")
        offset += length
        notes, tempos = _parse_track(track, track_number)
        all_notes.extend(notes)
        # Keep ordering deterministic when multiple tracks set tempo at one tick.
        all_tempos.extend((tick, tempo, track_number * 1_000_000 + order)
                          for tick, tempo, order in tempos)

    return MidiSong(division, tuple(all_notes), tuple(all_tempos))


class _TempoMap:
    def __init__(self, ticks_per_beat: int, events: Iterable[tuple[int, int, int]]) -> None:
        by_tick: dict[int, tuple[int, int]] = {}
        for tick, tempo, order in sorted(events, key=lambda item: (item[0], item[2])):
            by_tick[tick] = (tempo, order)

        self.ticks: list[int] = [0]
        self.milliseconds: list[float] = [0.0]
        self.tempos: list[int] = [500_000]
        last_tick, elapsed, tempo = 0, 0.0, 500_000
        for tick, (new_tempo, _) in sorted(by_tick.items()):
            if tick < 0 or new_tempo <= 0:
                continue
            if tick == 0:
                self.tempos[0] = tempo = new_tempo
                continue
            elapsed += (tick - last_tick) * tempo / ticks_per_beat / 1000.0
            self.ticks.append(tick)
            self.milliseconds.append(elapsed)
            self.tempos.append(new_tempo)
            last_tick, tempo = tick, new_tempo
        self.ticks_per_beat = ticks_per_beat

    def milliseconds_at(self, tick: int) -> float:
        index = bisect.bisect_right(self.ticks, tick) - 1
        return (self.milliseconds[index]
                + (tick - self.ticks[index]) * self.tempos[index]
                / self.ticks_per_beat / 1000.0)


def _choose_waveform(note: Note, requested: str) -> int:
    if requested != "auto":
        return WAVEFORMS[requested]
    if note.channel == 9:
        return WAVEFORMS["noise"]
    if note.note < 52 or 32 <= note.program <= 39:
        return WAVEFORMS["triangle"]
    if 80 <= note.program <= 87 or note.channel % 4 == 3:
        return WAVEFORMS["saw"]
    return WAVEFORMS["square"]


def _limit_polyphony(notes: Sequence[Note], maximum: int) -> list[Note]:
    """Keep at most ``maximum`` simultaneous notes, favoring louder notes."""
    selected: list[Note] = []
    active: list[int] = []
    for note in sorted(notes, key=lambda n: (n.start_ms, -n.velocity, n.note)):
        active = [index for index in active if selected[index].end_ms > note.start_ms]
        if len(active) >= maximum:
            victim = min(active, key=lambda index: (selected[index].velocity,
                                                     -selected[index].end_ms))
            if selected[victim].velocity >= note.velocity:
                continue
            selected[victim] = replace(selected[victim], end_ms=note.start_ms)
            active.remove(victim)
        selected.append(note)
        active.append(len(selected) - 1)
    return [note for note in selected if note.end_ms > note.start_ms]


def make_chip_song(
    midi: MidiSong,
    *,
    quantum_ms: int = 5,
    max_polyphony: int = 4,
    transpose: int = 0,
    waveform: str = "auto",
    minimum_velocity: int = 1,
) -> ChipSong:
    if not 1 <= quantum_ms <= 255:
        raise ValueError("quantum_ms must be between 1 and 255")
    if not 1 <= max_polyphony <= 32:
        raise ValueError("max_polyphony must be between 1 and 32")
    if waveform != "auto" and waveform not in WAVEFORMS:
        raise ValueError(f"unknown waveform: {waveform}")
    if not 1 <= minimum_velocity <= 127:
        raise ValueError("minimum_velocity must be between 1 and 127")

    tempo_map = _TempoMap(midi.ticks_per_beat, midi.tempos)
    timed: list[Note] = []
    for source in midi.notes:
        pitch = source.note + transpose
        if not 0 <= pitch <= 127 or source.velocity < minimum_velocity:
            continue
        note = Note(tempo_map.milliseconds_at(source.start_tick),
                    tempo_map.milliseconds_at(source.end_tick), pitch,
                    source.velocity, source.channel, source.program)
        timed.append(replace(note, waveform=_choose_waveform(note, waveform)))
    return ChipSong(quantum_ms, max_polyphony,
                    tuple(_limit_polyphony(timed, max_polyphony)))


def encode_chip(song: ChipSong) -> bytes:
    """Encode a song in the companion player's compact CHP1 format."""
    notes = sorted(song.notes, key=lambda note: (note.start_ms, note.note))
    out = bytearray(MAGIC)
    out.extend((song.quantum_ms, song.max_polyphony))
    out.extend(_encode_varint(len(notes)))
    previous_start = 0
    for note in notes:
        start = max(previous_start, round(note.start_ms / song.quantum_ms))
        duration = max(1, round((note.end_ms - note.start_ms) / song.quantum_ms))
        velocity_5bit = max(1, min(31, round(note.velocity * 31 / 127)))
        out.extend(_encode_varint(start - previous_start))
        out.extend(_encode_varint(duration))
        out.append(note.note)
        out.append((velocity_5bit << 2) | (note.waveform & 0x03))
        previous_start = start
    return bytes(out)


def convert_file(source: Path, destination: Path, args: argparse.Namespace) -> dict[str, object]:
    midi = parse_midi(source.read_bytes())
    song = make_chip_song(midi, quantum_ms=args.quantum_ms,
                          max_polyphony=args.max_polyphony,
                          transpose=args.transpose, waveform=args.waveform,
                          minimum_velocity=args.minimum_velocity)
    encoded = encode_chip(song)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(encoded)
    return {"title": source.stem, "file": destination.name,
            "durationMs": song.duration_ms, "notes": len(song.notes),
            "bytes": len(encoded)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert Standard MIDI files to compact Web Audio chiptunes.")
    parser.add_argument("inputs", nargs="+", type=Path, help="one or more .mid/.midi files")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("-o", "--output", type=Path,
                        help="output .chip file (only valid with one input)")
    output.add_argument("-d", "--output-dir", type=Path,
                        help="directory for converted files")
    parser.add_argument("--quantum-ms", type=int, default=5,
                        help="timing precision in milliseconds (default: 5)")
    parser.add_argument("--max-polyphony", type=int, default=4,
                        help="maximum simultaneous notes (default: 4)")
    parser.add_argument("--transpose", type=int, default=0, help="pitch shift in semitones")
    parser.add_argument("--minimum-velocity", type=int, default=1,
                        help="drop MIDI notes quieter than this (default: 1)")
    parser.add_argument("--waveform", choices=("auto", *WAVEFORMS), default="auto",
                        help="timbre selection (default: auto)")
    parser.add_argument("--no-manifest", action="store_true",
                        help="do not write manifest.json for multi-file/output-dir conversion")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.output and len(args.inputs) != 1:
        parser.error("--output can only be used with one input")
    missing = [str(path) for path in args.inputs if not path.is_file()]
    if missing:
        parser.error("input file not found: " + ", ".join(missing))

    if args.output:
        destinations = [args.output]
    elif args.output_dir:
        destinations = [args.output_dir / f"{path.stem}.chip" for path in args.inputs]
    elif len(args.inputs) == 1:
        destinations = [args.inputs[0].with_suffix(".chip")]
    else:
        output_dir = Path("chip_output")
        destinations = [output_dir / f"{path.stem}.chip" for path in args.inputs]

    manifest = []
    try:
        for source, destination in zip(args.inputs, destinations):
            item = convert_file(source, destination, args)
            manifest.append(item)
            print(f"{source} -> {destination} ({item['notes']} notes, {item['bytes']} bytes)")
    except (OSError, MidiError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    if not args.no_manifest and (args.output_dir or len(args.inputs) > 1):
        manifest_dir = args.output_dir or Path("chip_output")
        manifest_path = manifest_dir / "manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        print(f"manifest -> {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
