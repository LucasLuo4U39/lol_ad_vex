import struct
import unittest

import unpack_replay as target


def make_container_with_chunks() -> bytes:
    version = b"16.22.836.1077"
    header = (
        b"RIOT"
        + struct.pack("<H", 2)
        + b"\x00" * 8
        + bytes([len(version)])
        + version
    )

    first_data = b"hello-chunk"
    second_data = b"bye"

    def chunk(chunk_id, next_id, chunk_type, data):
        return (
            struct.pack("<IIBII", chunk_id, next_id, chunk_type, len(data), 0)
            + b"\x00"
            + data
        )

    return header + chunk(1, 2, 3, first_data) + chunk(2, 0, 4, second_data)


class ContainerTests(unittest.TestCase):
    def test_parse_container_header(self):
        buffer = make_container_with_chunks()
        game_version, header_end = target.parse_container_header(buffer)

        self.assertEqual(game_version, "16.22.836.1077")
        self.assertEqual(header_end, 29)

    def test_detect_chunks_uses_18_byte_header(self):
        buffer = make_container_with_chunks()
        _, header_end = target.parse_container_header(buffer)
        size, chunks = target.detect_chunks(buffer, header_end, len(buffer))

        self.assertEqual(size, 18)
        self.assertEqual([chunk.id for chunk in chunks], [1, 2])
        self.assertEqual(chunks[0].data, b"hello-chunk")
        self.assertEqual(chunks[1].type_name, "startup-end")


class BlockTests(unittest.TestCase):
    def test_absolute_and_relative_blocks(self):
        payload = b"\x01\x02\x03"
        absolute = (
            struct.pack("<BfIHI", 0, 12.5, len(payload), 302, 0x4000003D) + payload
        )
        relative = bytes([0xF0, 10, 1, 2]) + b"\x09"

        chunk = target.Chunk(
            sequence=0,
            id=1,
            next_id=2,
            type=2,
            header_size=18,
            offset=0,
            data_offset=18,
            stored_length=0,
            uncompressed_length=0,
            compressed_length=0,
            decompressed=absolute + relative,
        )

        blocks = target.parse_blocks(chunk)

        self.assertEqual(len(blocks), 2)
        self.assertAlmostEqual(blocks[0].timestamp, 12.5, places=3)
        self.assertEqual(blocks[0].packet_id, 302)
        self.assertEqual(blocks[0].param, 0x4000003D)
        self.assertEqual(blocks[0].payload, payload)
        self.assertAlmostEqual(blocks[1].timestamp, 12.51, places=3)
        self.assertEqual(blocks[1].packet_id, 302)
        self.assertEqual(blocks[1].param, 0x4000003F)
        self.assertEqual(blocks[1].payload, b"\x09")


class FormatTests(unittest.TestCase):
    def test_duration(self):
        self.assertEqual(target.format_duration(1860217), "31:00")
        self.assertEqual(target.format_duration(0), "0:00")


if __name__ == "__main__":
    unittest.main()
