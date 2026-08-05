import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from midi_to_chip import (  # noqa: E402
    ChipSong,
    MidiError,
    Note,
    convert_file,
    encode_chip,
    main,
    make_chip_song,
    parse_midi,
)


def varlen(value):
    encoded = bytearray((value & 0x7F,))
    value >>= 7
    while value:
        encoded.append(0x80 | (value & 0x7F))
        value >>= 7
    encoded.reverse()
    return bytes(encoded)


def midi_file(track, *, midi_format=0, division=480):
    header = b"MThd" + struct.pack(">IHHH", 6, midi_format, 1, division)
    return header + b"MTrk" + struct.pack(">I", len(track)) + track


def read_varint(data, offset):
    value = 0
    while True:
        byte = data[offset]
        offset += 1
        value = value * 128 + (byte & 0x7F)
        if byte < 0x80:
            return value, offset


def decode_for_test(data):
    assert data[:4] == b"CHP1"
    count, offset = read_varint(data, 6)
    notes = []
    start = 0
    for _ in range(count):
        delta, offset = read_varint(data, offset)
        duration, offset = read_varint(data, offset)
        start += delta
        pitch, packed = data[offset : offset + 2]
        offset += 2
        notes.append((start, duration, pitch, packed >> 2, packed & 3))
    return data[4], data[5], notes, offset


class MidiParsingTests(unittest.TestCase):
    def test_parses_running_status_and_tempo_changes(self):
        track = b"".join([
            b"\x00\xff\x51\x03\x07\xa1\x20",  # 120 bpm
            b"\x00\x90\x3c\x64",              # C4 on
            varlen(480), b"\x3c\x00",           # running-status note-off
            b"\x00\xff\x51\x03\x0f\x42\x40",  # 60 bpm
            b"\x00\x90\x40\x50",              # E4 on
            varlen(480), b"\x80\x40\x00",
            b"\x00\xff\x2f\x00",
        ])
        midi = parse_midi(midi_file(track))
        song = make_chip_song(midi)

        self.assertEqual([note.note for note in song.notes], [60, 64])
        self.assertAlmostEqual(song.notes[0].end_ms, 500)
        self.assertAlmostEqual(song.notes[1].start_ms, 500)
        self.assertAlmostEqual(song.notes[1].end_ms, 1500)

    def test_rejects_smpte_timing(self):
        track = b"\x00\xff\x2f\x00"
        with self.assertRaisesRegex(MidiError, "SMPTE"):
            parse_midi(midi_file(track, division=0xE728))

    def test_closes_note_missing_note_off_at_end_of_track(self):
        track = b"\x00\x90\x3c\x50" + varlen(240) + b"\xff\x2f\x00"
        midi = parse_midi(midi_file(track))
        self.assertEqual(len(midi.notes), 1)
        self.assertEqual((midi.notes[0].start_tick, midi.notes[0].end_tick), (0, 240))


class ConversionTests(unittest.TestCase):
    def test_polyphony_limit_keeps_louder_notes(self):
        notes = (
            Note(0, 1000, 60, 30, 0, 0),
            Note(0, 1000, 64, 100, 1, 0),
            Note(0, 1000, 67, 70, 2, 0),
        )
        # Create a MIDI-like object at 1 ms per tick to exercise the public API.
        from midi_to_chip import MidiSong, TickNote
        midi = MidiSong(1000, tuple(
            TickNote(int(n.start_ms), int(n.end_ms), n.note, n.velocity, n.channel, n.program)
            for n in notes), ((0, 1_000_000, 0),))
        song = make_chip_song(midi, max_polyphony=2)
        self.assertEqual({note.note for note in song.notes}, {64, 67})

    def test_encoder_quantizes_and_packs_notes(self):
        song = ChipSong(5, 4, (
            Note(0, 502, 60, 127, 0, 0, 0),
            Note(510, 1010, 64, 64, 1, 0, 1),
        ))
        encoded = encode_chip(song)
        quantum, polyphony, notes, end_offset = decode_for_test(encoded)

        self.assertEqual((quantum, polyphony), (5, 4))
        self.assertEqual(notes, [(0, 100, 60, 31, 0), (102, 100, 64, 16, 1)])
        self.assertEqual(end_offset, len(encoded))

    def test_convert_file_writes_a_chip_file(self):
        track = b"\x00\x90\x3c\x64" + varlen(480) + b"\x80\x3c\x00\x00\xff\x2f\x00"
        args = SimpleNamespace(quantum_ms=5, max_polyphony=4, transpose=0,
                               waveform="auto", minimum_velocity=1)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "tune.mid"
            destination = Path(directory) / "music" / "tune.chip"
            source.write_bytes(midi_file(track))
            item = convert_file(source, destination, args)

            self.assertTrue(destination.read_bytes().startswith(b"CHP1"))
            self.assertEqual(item["notes"], 1)
            self.assertEqual(item["file"], "tune.chip")

    def test_multi_file_cli_writes_files_and_manifest(self):
        track = b"\x00\x90\x3c\x64" + varlen(120) + b"\x80\x3c\x00\x00\xff\x2f\x00"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = [root / "one.mid", root / "two.midi"]
            for source in sources:
                source.write_bytes(midi_file(track))
            output = root / "music"

            result = main([*(str(path) for path in sources), "--output-dir", str(output)])

            self.assertEqual(result, 0)
            self.assertTrue((output / "one.chip").is_file())
            self.assertTrue((output / "two.chip").is_file())
            manifest = json.loads((output / "manifest.json").read_text())
            self.assertEqual([item["title"] for item in manifest], ["one", "two"])


if __name__ == "__main__":
    unittest.main()
