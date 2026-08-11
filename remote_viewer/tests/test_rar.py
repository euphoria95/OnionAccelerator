"""Unit tests for the hand-rolled RAR parser: its guards, and the parts unrar cannot reach.

Everything here runs against a ``BytesIO`` with no server and no corpus. The subject is
the malformed input a remote archive is allowed to be — a header claiming to be three
bytes long, a name length larger than the whole file, a variable-length integer that
never terminates. Each must fail loudly, and none may send the walker backwards, off the
end, or round in a circle: an unbounded walk here is unbounded transfer over Tor.
"""

from __future__ import annotations

import io
import struct

import pytest
import rarwriter

from rvtree.formats import rar


def _blob(data: bytes):
    fp = io.BytesIO(data)
    fp.size = len(data)  # type: ignore[attr-defined]
    return fp


def _rar5(*blocks: bytes):
    return _blob(rarwriter.SIG5 + rarwriter._block5(1, 0, rarwriter.vint(0)) + b"".join(blocks))


def _file5(name: str, data: bytes = b"", file_flags: int = 0) -> bytes:
    fields = (
        rarwriter.vint(file_flags)
        + rarwriter.vint(len(data))
        + rarwriter.vint(0o100644)
        + rarwriter.vint(0)          # compression info: store
        + rarwriter.vint(1)          # host OS: Unix
        + rarwriter.vint(len(name.encode()))
        + name.encode()
    )
    flags = 0x0002 if data else 0
    return rarwriter._block5(2, flags, fields, data_size=len(data)) + data


# ----------------------------------------------------------------- vints


def test_vint_round_trips():
    for value in (0, 1, 127, 128, 300, 2**31, 2**63 - 1):
        assert rar._vint(rarwriter.vint(value), 0) == (value, len(rarwriter.vint(value)))


def test_an_endless_vint_is_rejected():
    """Eleven continuation bytes is corruption, not a very large number."""
    with pytest.raises(rar.RarError, match="over-long"):
        rar._vint(b"\x80" * 11, 0)


def test_a_truncated_vint_is_rejected():
    with pytest.raises(rar.RarError, match="truncated"):
        rar._vint(b"\x80\x80", 0)


# ----------------------------------------------------------------- RAR4 names


@pytest.mark.parametrize(
    "encoded,expected",
    [
        # Opcode 0: one raw byte, no high byte applied.
        (bytes([0x00, 0x00, 0x41]), "A"),
        # Opcode 1: one byte plus the shared high byte.
        (bytes([0x04, 0x40, 0x41]), chr(0x441)),
        # Opcode 2: a raw little-endian UTF-16 unit.
        (bytes([0x00, 0x80, 0xE9, 0x00]), "é"),
        # Opcode 3, plain form: copy length+2 characters from the raw field at index 0.
        (bytes([0x00, 0xC0, 0x00]), "ab"),
        # Opcode 3, correction form: the same, each byte shifted by a correction.
        (bytes([0x00, 0xC0, 0x80, 0x01]), "bc"),
    ],
)
def test_rar4_name_opcodes(encoded, expected):
    """All four opcodes of RAR4's name encoding. The fixtures only ever exercise one."""
    assert rar._decode_name4(b"ab\x00" + encoded, True) == expected


def test_a_malformed_rar4_name_degrades_instead_of_raising():
    """A bad name must never abort a listing that is otherwise fine."""
    assert rar._decode_name4(b"plain.txt\x00\x00\xff", True) is not None


def test_a_rar4_name_without_the_separator_is_read_as_utf8():
    assert rar._decode_name4("naïve.txt".encode(), True) == "naïve.txt"


def test_a_rar4_name_without_the_unicode_flag_falls_back_to_oem():
    """A pre-unicode name is OEM cp437, where 'é' is 0x82 and 0xe9 would be 'Θ'."""
    assert rar._decode_name4("été.txt".encode("cp437"), False) == "été.txt"
    assert rar._decode_name4("naïve.txt".encode("utf-8"), False) == "naïve.txt"


# ----------------------------------------------------------------- signatures


def test_signature_is_found_at_offset_zero():
    assert rar.find_signature(_blob(rarwriter.SIG5 + b"\x00" * 32)) == (0, 5)
    assert rar.find_signature(_blob(rarwriter.SIG4 + b"\x00" * 32)) == (0, 4)


def test_a_self_extracting_stub_is_scanned_past():
    """An SFX archive puts an executable in front of the signature."""
    stub = b"MZ" + b"\x90" * 50_000
    assert rar.find_signature(_blob(stub + rarwriter.SIG4 + b"\x00" * 32)) == (len(stub), 4)


def test_a_file_with_no_signature_says_so():
    with pytest.raises(rar.RarError, match="not a RAR archive"):
        rar.find_signature(_blob(b"PK\x03\x04" + b"\x00" * 400))


# ----------------------------------------------------------------- walk guards


def test_a_zero_length_rar4_header_is_rejected():
    """HEAD_SIZE below the base header would leave the walk standing still."""
    body = struct.pack("<BHH", 0x74, 0x8000, 3) + b"\x00" * 8
    blob = _blob(rarwriter.SIG4 + b"\x00\x00" + body + b"\x00" * 64)
    with pytest.raises(rar.RarError, match="3-byte header"):
        rar.read_info(blob)


def test_a_header_running_past_the_end_is_rejected():
    good = rarwriter.SIG5 + rarwriter._block5(1, 0, rarwriter.vint(0))
    truncated = _blob(good + b"\x00\x00\x00\x00\xff\x7f")  # claims a 16 KiB header
    with pytest.raises(rar.RarError, match="past the end"):
        list(rar.walk(truncated))


def test_a_data_size_past_the_end_stops_the_walk_loudly():
    """A member claiming more data than the archive holds must not be walked past."""
    fields = (
        rarwriter.vint(0) + rarwriter.vint(1 << 30) + rarwriter.vint(0o100644)
        + rarwriter.vint(0) + rarwriter.vint(1) + rarwriter.vint(1) + b"x"
    )
    blob = _rar5(rarwriter._block5(2, 0x0002, fields, data_size=1 << 30))
    with pytest.raises(rar.RarError, match="corrupt RAR header chain"):
        list(rar.walk(blob))


def test_an_enormous_name_length_is_rejected():
    """NameLength is a vint and can claim gigabytes; the header itself cannot hold them."""
    fields = (
        rarwriter.vint(0) + rarwriter.vint(0) + rarwriter.vint(0o100644)
        + rarwriter.vint(0) + rarwriter.vint(1) + rarwriter.vint(1 << 20) + b"x"
    )
    blob = _rar5(rarwriter._block5(2, 0, fields))
    with pytest.raises(rar.RarError, match="byte name"):
        list(rar.walk(blob))


# ----------------------------------------------------------------- header semantics


def test_service_records_are_not_listed_as_members():
    """RAR5 type 3 shares the file header's layout exactly; yielding it invents members."""
    quick_open = _file5("QO", b"cached headers would go here")
    blob = _rar5(quick_open.replace(b"\x02\x02", b"\x03\x02", 1), _file5("real.txt", b"hi"))
    assert [e.path for e in rar.walk(blob)] == ["real.txt"]


def test_rar4_newsub_records_are_not_listed_as_members():
    """0x7a has FILE's exact layout, so a naive walk emits phantom CMT/RR/ACL entries."""
    def block(htype, name, data=b""):
        tail = struct.pack(
            "<IIBIIBBHI", len(data), len(data), 3, 0, 0, 20, 0x30, len(name), 0o100644
        )
        return rarwriter._block4(htype, 0x8000, tail + name) + data

    blob = _blob(
        rarwriter.SIG4
        + rarwriter._block4(0x73, 0, struct.pack("<HI", 0, 0))
        + block(0x7A, b"CMT", b"a comment")
        + block(0x74, b"real.txt", b"hi")
        + rarwriter._block4(0x7B, 0, b"")
    )
    assert [e.path for e in rar.walk(blob)] == ["real.txt"]


def test_an_unknown_unpacked_size_reports_zero_and_still_advances():
    """FileFlags 0x0008 means the encoder did not know the size; only DataSize is real."""
    blob = _rar5(_file5("streamed.bin", b"0123456789", file_flags=0x0008), _file5("after.txt"))
    entries = list(rar.walk(blob))
    assert [e.path for e in entries] == ["streamed.bin", "after.txt"]
    assert entries[0].size == 0
    assert entries[0].csize == 10


def test_encrypted_headers_refuse_before_any_parsing():
    crypt = rarwriter._block5(4, 0, rarwriter.vint(0) + rarwriter.vint(0) + b"\x0f" + b"\x00" * 16)
    blob = _blob(rarwriter.SIG5 + crypt)
    with pytest.raises(rar.EncryptedArchive, match="password"):
        rar.read_info(blob)


def test_a_path_escaping_the_archive_is_refused():
    """The helper writes real paths, and a remote archive is untrusted input."""
    for bad in ("../etc/passwd", "/etc/passwd", "C:\\windows\\system32"):
        with pytest.raises(rar.RarError, match="escapes the archive"):
            rar._check_member_path(bad)


def test_limit_stops_the_walk_early():
    blob = _rar5(_file5("a.txt", b"a"), _file5("b.txt", b"b"), _file5("c.txt", b"c"))
    assert [e.path for e in rar.walk(blob, limit=2)] == ["a.txt", "b.txt"]
