"""TLS policy: unverified by default, verified only when asked.

An onion address is the service's public key, so it already authenticates the peer and
onion services almost never carry a certificate a CA would sign. Verifying by default
would make HTTPS onions unreachable, so rvtree does not — and these tests pin both the
default and the way back to a verified connection.
"""

from __future__ import annotations

import os

import pytest
import requests

from rvtree import archive
from rvtree.transport import DEFAULT_VERIFY, Transport, TransportError

CERT = "selfsigned.pem"  # key + cert, for the server
CA = "selfsigned.crt"  # cert only, usable as a client CA bundle


@pytest.fixture
def certs(fixtures):
    pem = os.path.join(fixtures, CERT)
    if not os.path.isfile(pem):
        pytest.skip("no self-signed certificate in the fixtures (openssl missing?)")
    return pem, os.path.join(fixtures, CA)


@pytest.fixture
def tls_server(fixtures, certs):
    from rangeserver import RangeServer

    with RangeServer(fixtures, certfile=certs[0]) as srv:
        yield srv


def _transport(**kwargs):
    return Transport(proxy=None, circuits=1, **kwargs)


# ----------------------------------------------------------------- configuration


def test_verification_is_off_by_default():
    assert DEFAULT_VERIFY is False
    t = _transport()
    try:
        assert t.verify is False
        circuit = t._acquire()
        assert circuit.verify is False
        assert circuit.session.verify is False
    finally:
        t.close()


@pytest.mark.parametrize("verify", [True, "/etc/ssl/certs/ca-certificates.crt"])
def test_verification_reaches_every_circuit_when_asked(verify):
    t = Transport(proxy=None, circuits=3, verify=verify)
    try:
        assert t.verify == verify
        for _ in range(3):
            assert t._acquire().session.verify == verify
    finally:
        t.close()


def test_verify_is_passed_on_every_request(transport, server, monkeypatch):
    """Setting it on the session is not enough, so assert it travels with the call.

    ``Session.merge_environment_settings`` lets REQUESTS_CA_BUNDLE and CURL_CA_BUNDLE
    override a session-level ``verify=False``. Only an explicit per-request value wins.
    """
    seen = []
    circuit = transport._acquire()
    transport._release(circuit)
    original = circuit.session.get

    def spy(*args, **kwargs):
        seen.append(kwargs.get("verify", "<missing>"))
        return original(*args, **kwargs)

    monkeypatch.setattr(circuit.session, "get", spy)

    arc = archive.open_archive(transport, f"{server.base}/test.zip")
    list(archive.list_archive(arc))

    assert seen, "no requests were made"
    assert set(seen) == {False}


def test_ca_bundle_env_vars_cannot_re_enable_verification(certs, tls_server, monkeypatch):
    """The functional half of the above: a hostile env must not break the default."""
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/nonexistent/ca.pem")
    monkeypatch.setenv("CURL_CA_BUNDLE", "/nonexistent/ca.pem")
    t = _transport(retries=1)
    try:
        arc = archive.open_archive(t, f"{tls_server.base}/test.zip")
        assert len(list(archive.list_archive(arc))) == 14
    finally:
        t.close()


# ----------------------------------------------------------------- against a real server


def test_self_signed_certificate_is_accepted_by_default(tls_server):
    t = _transport()
    try:
        arc = archive.open_archive(t, f"{tls_server.base}/test.zip")
        assert arc.fmt == "zip"
        assert len(list(archive.list_archive(arc))) == 14
    finally:
        t.close()


def test_self_signed_certificate_is_refused_when_verifying(tls_server):
    t = _transport(verify=True, retries=1)
    try:
        with pytest.raises(TransportError) as exc:
            archive.open_archive(t, f"{tls_server.base}/test.zip")
        assert "--verify-tls" in str(exc.value)
    finally:
        t.close()


def test_a_ca_bundle_verifies_the_certificate_that_signed_it(certs, tls_server):
    """--ca-bundle is the middle path: verify, but against a CA you chose."""
    t = _transport(verify=certs[1])
    try:
        arc = archive.open_archive(t, f"{tls_server.base}/test.zip")
        assert len(list(archive.list_archive(arc))) == 14
    finally:
        t.close()


def test_tls_failure_reaches_the_cli_as_a_message_not_a_traceback(tls_server, capsys):
    """The whole point of wrapping it: a rejected certificate is a user error, not a crash."""
    from rvtree.cli import main

    code = main(
        ["probe", "--proxy", "none", "--verify-tls", "--retries", "1", f"{tls_server.base}/test.zip"]
    )
    err = capsys.readouterr().err
    assert code == 1
    assert err.startswith("rvtree: TLS verification failed")
    assert "Traceback" not in err


def test_certificate_rejection_is_not_retried(monkeypatch):
    """Verification is a verdict, not a hiccup: retrying only adds backoff to the answer."""
    t = Transport(proxy=None, circuits=1, verify=True, retries=3)
    circuit = t._acquire()
    t._release(circuit)
    attempts = []

    def refuse(*args, **kwargs):
        attempts.append(kwargs.get("verify"))
        raise requests.exceptions.SSLError("certificate verify failed: self-signed certificate")

    monkeypatch.setattr(circuit.session, "get", refuse)
    try:
        with pytest.raises(TransportError, match="--verify-tls"):
            t.get_range("https://example.invalid/big.tar.xz", 0, 15)
        assert attempts == [True], "a rejected certificate was retried"
    finally:
        t.close()


# ----------------------------------------------------------------- cli wiring


@pytest.mark.parametrize(
    "argv,expected",
    [
        ([], False),
        (["--verify-tls"], True),
        (["--ca-bundle", "/tmp/ca.crt"], "/tmp/ca.crt"),
        (["--verify-tls", "--ca-bundle", "/tmp/ca.crt"], "/tmp/ca.crt"),
    ],
)
def test_cli_flags_map_to_the_verify_setting(argv, expected):
    from rvtree.cli import build_parser

    args = build_parser().parse_args(["list", *argv, "http://example.invalid/a.zip"])
    assert (args.ca_bundle or args.verify_tls) == expected
