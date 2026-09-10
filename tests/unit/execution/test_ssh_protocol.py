import pytest

from athena.execution.ssh_protocol import (
    MAX_FRAME_BYTES,
    PROTOCOL_NAME,
    PROTOCOL_VERSION,
    SSHProtocolError,
    encode_frame,
    decode_frame,
    handshake,
    parse_length_line,
    validate_handshake,
)


def test_supervisor_handshake_is_versioned_and_validated():
    value = handshake("secret", "describe")
    assert value["protocol"] == PROTOCOL_NAME
    assert value["version"] == PROTOCOL_VERSION
    validate_handshake(value)

    with pytest.raises(SSHProtocolError, match="unsupported"):
        validate_handshake({**value, "version": PROTOCOL_VERSION + 1})


def test_supervisor_frames_are_bounded_and_reject_hostile_lengths():
    frame = encode_frame({"source": "print(1)"})
    assert parse_length_line(frame.splitlines(keepends=True)[0]) == len(frame.split(b"\n", 1)[1])
    with pytest.raises(SSHProtocolError, match="maximum"):
        parse_length_line(f"{MAX_FRAME_BYTES + 1}\n".encode())
    with pytest.raises(SSHProtocolError, match="invalid"):
        parse_length_line(b"9" * 65 + b"\n")
    with pytest.raises(SSHProtocolError, match="maximum"):
        encode_frame({"source": "x" * MAX_FRAME_BYTES})


def test_supervisor_handshake_binds_runtime_worker_and_authority_identity():
    value = handshake(
        "secret",
        "stream",
        session_id="session-1",
        task_id="task-1",
        runtime="python",
        runtime_identity="python:/usr/bin/python3:3.12",
        worker_source_sha="a" * 64,
        session_nonce="nonce-1",
        authority_digest="b" * 64,
    )
    validate_handshake(value)
    with pytest.raises(SSHProtocolError, match="incomplete"):
        validate_handshake({**value, "session_nonce": ""})
    with pytest.raises(SSHProtocolError, match="SHA-256"):
        validate_handshake({**value, "authority_digest": "not-a-digest"})


def test_supervisor_frame_decoder_rejects_truncation_and_invalid_utf8():
    frame = encode_frame({"op": "run", "source": "print(1)"})
    length, payload = frame.split(b"\n", 1)
    assert decode_frame(length + b"\n", payload)["op"] == "run"
    with pytest.raises(SSHProtocolError, match="truncated"):
        decode_frame(length + b"\n", payload[:-1])
    with pytest.raises(SSHProtocolError, match="invalid SSH supervisor JSON"):
        decode_frame(b"1\n", b"\xff")
