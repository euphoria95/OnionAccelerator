"""OnionAccelerator.py as a façade over oacore/, and the packaging around it.

The download modes, the size probe and the farm moved into oacore/, but every test and
older caller still reaches them as `OnionAccelerator.<name>` -- and many patch them
there. A moved function that is patched on the old module silently stops being patched:
the test keeps passing while the real network or systemd code runs underneath it. These
tests pin the forwarding that prevents that, and the entry points packaging adds.
"""

import ast
import os
import subprocess
import sys
import tomllib

import pytest
import requests

import fullhelp
import OnionAccelerator as oa
from oacore import common, config, download, farm, sizeprobe

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOVED = (config, common, sizeprobe, download, farm)

# Names the suite patches through `oa` today. Each must be forwarded, or the patch
# misses the module whose code actually reads it.
PATCHED_BY_TESTS = (
    "make_proxies", "_probe", "DOWNLOAD_DIR",
    "farm_preflight", "wait_bootstrap", "_read_torrc", "systemd_active", "_run",
    "_write_root_file", "farm_up",
)


def _own_names(module):
    """Top-level names a moved module defines itself, as opposed to ones it imports."""
    with open(module.__file__, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            yield node.name
        elif isinstance(node, ast.Assign):
            yield from (t.id for t in node.targets if isinstance(t, ast.Name))


@pytest.mark.parametrize("module", MOVED, ids=lambda m: m.__name__)
def test_every_moved_name_is_reachable_on_the_old_module(module):
    for name in _own_names(module):
        assert getattr(oa, name) is getattr(module, name), name


@pytest.mark.parametrize("name", PATCHED_BY_TESTS)
def test_names_the_suite_patches_are_forwarded(name):
    assert name in oa._FORWARDED


def test_every_forwarded_patch_reaches_each_home_and_is_undone(monkeypatch):
    sentinel = object()
    originals = {name: [getattr(m, name) for m in homes]
                 for name, homes in oa._FORWARDED.items()}
    with monkeypatch.context() as patch:
        for name, homes in oa._FORWARDED.items():
            patch.setattr(oa, name, sentinel)
            for module in homes:
                assert getattr(module, name) is sentinel, (module.__name__, name)
    for name, homes in oa._FORWARDED.items():
        assert [getattr(m, name) for m in homes] == originals[name], name
        assert getattr(oa, name) is originals[name][0], name


def test_a_patch_on_the_old_module_reaches_code_in_the_new_one(monkeypatch):
    """The end-to-end form of the above: patch via oa, call the moved function."""
    calls = []

    def fake_probe(session, url, method, extra_headers=None, stream=False):
        calls.append(method)
        raise requests.ConnectionError("no network in this test")

    monkeypatch.setattr(oa, "make_proxies", lambda proxy: {})
    monkeypatch.setattr(oa, "_probe", fake_probe)
    assert sizeprobe.probe_remote_size("http://x.onion/f", "127.0.0.1:9").size is None
    assert calls, "probe_remote_size called the real _probe, not the patched one"


# ----------------------------------------------------------------- farm safety


@pytest.mark.parametrize("name", sorted(farm.FARM_RESERVED) + ["oa", "oa1x", "tor", "xoa01"])
def test_the_farm_refuses_every_name_it_does_not_own(name):
    with pytest.raises(ValueError):
        farm.assert_farm_name(name)


@pytest.mark.parametrize("name", ["oa00", "oa07", "oa19"])
def test_the_farm_accepts_its_own_instance_names(name):
    assert farm.assert_farm_name(name) == name


# ----------------------------------------------------------------- entry points


def test_the_console_script_is_the_scripts_main():
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as fh:
        project = tomllib.load(fh)["project"]
    target = project["scripts"]["onionaccelerator"]
    module, _, attr = target.partition(":")
    assert module == "OnionAccelerator" and getattr(oa, attr) is oa.main


def test_running_the_script_prints_the_same_full_help():
    """`python3 OnionAccelerator.py` keeps working, and as a façade prints the same text."""
    out = subprocess.run([sys.executable, os.path.join(ROOT, "OnionAccelerator.py"),
                          "--full-help"], capture_output=True, text=True, check=True,
                         cwd=ROOT, timeout=60)
    assert out.stdout == fullhelp.render(None)


def test_every_package_directory_is_packaged():
    """setuptools is given an explicit list; a new subpackage must not be left out of it."""
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as fh:
        setuptools = tomllib.load(fh)["tool"]["setuptools"]
    found = set()
    for top, base in (("crawler", ROOT), ("eventstream", ROOT), ("oacore", ROOT),
                      ("rvtree", os.path.join(ROOT, "remote_viewer"))):
        for dirpath, _, files in os.walk(os.path.join(base, top)):
            if "__init__.py" in files:
                found.add(os.path.relpath(dirpath, base).replace(os.sep, "."))
    assert set(setuptools["packages"]) == found


def test_an_installed_rvtree_is_preferred_over_the_checkout(monkeypatch):
    """`pip install .` puts rvtree on the path; the checkout fallback must not shadow it."""
    import types

    fake = types.ModuleType("rvtree")
    fake.__path__ = []
    fake.cli = types.ModuleType("rvtree.cli")
    fake.transport = types.ModuleType("rvtree.transport")
    fake.transport.PooledTransport = object()
    for name, module in (("rvtree", fake), ("rvtree.cli", fake.cli),
                         ("rvtree.transport", fake.transport)):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(sys, "path", list(sys.path))
    before = list(sys.path)

    assert oa._import_rvtree() == (fake.cli, fake.transport.PooledTransport)
    assert sys.path == before
