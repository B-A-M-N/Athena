#!/usr/bin/env python3
"""Minimal pytest-compatible runner for sandboxed environments.

The Athena repo's tests are written for ``pytest`` (``uv run pytest``).  Some
execution environments (offline sandboxes) have neither ``uv`` nor network
access to install pytest.  This runner exists ONLY to make the test-suite
executable there; it is not a replacement for pytest in CI.

Supported subset (everything the CLI/UI tests need):

* ``test_*.py`` discovery, plain ``test_*`` functions and ``Test*`` classes
* ``async def`` tests (any ``@pytest.mark.asyncio`` mark, or bare async)
* fixtures: ``capsys``, ``tmp_path``, ``monkeypatch``
* ``pytest.raises`` (incl. ``match=``), ``pytest.mark.*`` no-op marks,
  ``pytest.mark.parametrize`` (single or stacked, argnames as str or list)
* ``-q``, ``-k EXPR`` (substring match), file path arguments

Usage::

    PYTHONPATH=src python3 scripts/dev_pytest.py -q tests/unit/cli
"""
from __future__ import annotations

import asyncio
import importlib.util
import inspect
import io
import os
import re
import sys
import tempfile
import traceback
import types
from contextlib import contextmanager
from pathlib import Path

# ---------------------------------------------------------------------------
# pytest API subset
# ---------------------------------------------------------------------------


class _RaisesContext:
    def __init__(self, expected, match=None):
        self.expected = expected
        self.match = match
        self.value = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            raise AssertionError(f"DID NOT RAISE {self.expected!r}")
        if not issubclass(exc_type, self.expected):
            return False
        if self.match and not re.search(self.match, str(exc)):
            raise AssertionError(
                f"exception message {exc!r} does not match {self.match!r}")
        self.value = exc
        return True


class _Mark:
    def __getattr__(self, name):
        if name == "parametrize":
            return self._parametrize

        def deco(func=None, *args, **kwargs):
            if func is not None and callable(func):
                return func
            return lambda f: f

        return deco

    @staticmethod
    def _parametrize(argnames, argvalues, **kwargs):
        if isinstance(argnames, str):
            names = [n.strip() for n in argnames.split(",") if n.strip()]
        else:
            names = list(argnames)

        def deco(func):
            existing = getattr(func, "_parametrize", [])
            existing.append((names, list(argvalues)))
            func._parametrize = existing
            return func

        return deco


class _MonkeyPatch:
    def __init__(self):
        self._undo = []

    def setattr(self, target, name=None, value=None, raising=True):
        if isinstance(target, str):
            # monkeypatch.setattr("mod.attr", value) dotted-path form
            dotted, value = target, name
            module_path, _, attr = dotted.rpartition(".")
            import importlib

            target = importlib.import_module(module_path)
            name = attr
        # Restore the *raw* class-dict entry (staticmethod/classmethod
        # descriptors survive; a bare function would rebind as a method).
        raw = target.__dict__[name] if isinstance(target, type) else getattr(target, name)
        self._undo.append(lambda t=target, n=name, r=raw: setattr(t, n, r))
        setattr(target, name, value)

    def setenv(self, name, value):
        old = os.environ.get(name)
        self._undo.append(
            (lambda: os.environ.__setitem__(name, old)) if old is not None
            else (lambda: os.environ.pop(name, None)))
        os.environ[name] = str(value)

    def delenv(self, name, raising=True):
        old = os.environ.pop(name, None)
        if old is not None:
            self._undo.append(lambda: os.environ.__setitem__(name, old))

    def undo(self):
        for undo in reversed(self._undo):
            undo()
        self._undo.clear()


class _Capsys:
    def __init__(self):
        self.out = io.StringIO()
        self.err = io.StringIO()
        self._old_out = None
        self._old_err = None

    def start(self):
        self._old_out, self._old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = self.out, self.err

    def stop(self):
        if self._old_out is not None:
            sys.stdout, sys.stderr = self._old_out, self._old_err
            self._old_out = self._old_err = None

    def readouterr(self):
        out, err = self.out.getvalue(), self.err.getvalue()
        self.out.seek(0)
        self.out.truncate(0)
        self.err.seek(0)
        self.err.truncate(0)
        return types.SimpleNamespace(out=out, err=err)


class _PytestModule(types.ModuleType):
    def __init__(self):
        super().__init__("pytest")
        self.mark = _Mark()
        self.MonkeyPatch = _MonkeyPatch
        self.fixture = lambda func=None, **kw: (
            func if func is not None else (lambda f: f))
        self.skip = lambda reason="": (_ for _ in ()).throw(Skipped(reason))
        self.fail = lambda reason="": (_ for _ in ()).throw(
            AssertionError(reason or "pytest.fail()"))
        self.approx = lambda x, **kw: x

    def raises(self, expected, match=None):
        return _RaisesContext(expected, match=match)


class Skipped(Exception):
    pass


def _install_pytest_shim():
    if "pytest" not in sys.modules:
        sys.modules["pytest"] = _PytestModule()


# ---------------------------------------------------------------------------
# Discovery & execution
# ---------------------------------------------------------------------------

FIXTURES = ("capsys", "tmp_path", "monkeypatch")


def _iter_test_funcs(module):
    for name in dir(module):
        obj = getattr(module, name)
        if name.startswith("test_") and callable(obj):
            yield name, obj
        elif (
            inspect.isclass(obj)
            and name.startswith("Test")
            and obj.__module__ == module.__name__
        ):
            for mname in dir(obj):
                if mname.startswith("test_"):
                    meth = getattr(obj, mname)
                    if callable(meth):
                        yield f"{name}.{mname}", meth


def _expand_parametrize(func):
    cases = getattr(func, "_parametrize", None)
    if not cases:
        yield "", {}
        return
    combos = [{}]
    suffixes = [""]
    for names, values in cases:
        new_combos, new_suffixes = [], []
        for i, val in enumerate(values):
            if len(names) == 1:
                val = (val,)
            for base, sfx in zip(combos, suffixes):
                merged = dict(base)
                for n, v in zip(names, val):
                    merged[n] = v
                new_combos.append(merged)
                new_suffixes.append(f"{sfx}[{i}]")
        combos, suffixes = new_combos, new_suffixes
    for sfx, kw in zip(suffixes, combos):
        yield sfx, kw


def _build_fixtures(func, params):
    sig = inspect.signature(func)
    kwargs = {}
    tmp_dirs = []
    monkeypatch = None
    capsys = None
    for name in sig.parameters:
        if name in params:
            kwargs[name] = params[name]
        elif name == "capsys":
            capsys = _Capsys()
            kwargs[name] = capsys
        elif name == "monkeypatch":
            monkeypatch = _MonkeyPatch()
            kwargs[name] = monkeypatch
        elif name == "tmp_path":
            d = tempfile.mkdtemp(prefix="devpytest-")
            tmp_dirs.append(d)
            kwargs[name] = Path(d)
        else:
            raise TypeError(
                f"unsupported fixture {name!r} in {func.__qualname__} "
                "(dev_pytest supports: capsys, tmp_path, monkeypatch)")
    return kwargs, capsys, monkeypatch, tmp_dirs


def _load_module(path: Path):
    mod_name = "devpytest_" + re.sub(r"\W", "_", str(path))
    spec = importlib.util.spec_from_file_location(mod_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def main(argv: list[str]) -> int:
    _install_pytest_shim()
    quiet = "-q" in argv
    kexpr = None
    paths = []
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "-k":
            i += 1
            kexpr = argv[i]
        elif not arg.startswith("-"):
            paths.append(arg)
        i += 1
    if not paths:
        paths = ["tests/unit/cli"]

    files: list[Path] = []
    for p in paths:
        pp = Path(p)
        if pp.is_dir():
            files.extend(sorted(pp.rglob("test_*.py")))
        elif pp.is_file():
            files.append(pp)

    passed = failed = skipped = 0
    failures: list[str] = []
    for path in files:
        try:
            module = _load_module(path)
        except Exception:
            failed += 1
            failures.append(f"COLLECT {path}\n{traceback.format_exc()}")
            continue
        for name, func in _iter_test_funcs(module):
            for suffix, params in _expand_parametrize(func):
                nodeid = f"{path}::{name}{suffix}"
                if kexpr and kexpr not in nodeid:
                    continue
                capsys = monkeypatch = None
                tmp_dirs = []
                try:
                    kwargs, capsys, monkeypatch, tmp_dirs = _build_fixtures(
                        func, params)
                except TypeError as exc:
                    skipped += 1
                    print(f"SKIP {nodeid} ({exc})")
                    continue
                if capsys:
                    capsys.start()
                try:
                    result = func(**kwargs)
                    if inspect.isawaitable(result):
                        asyncio.run(result)
                    passed += 1
                    if not quiet:
                        print(f"PASS {nodeid}")
                except Skipped as s:
                    skipped += 1
                    print(f"SKIP {nodeid} ({s})")
                except Exception:
                    failed += 1
                    failures.append(f"FAIL {nodeid}\n{traceback.format_exc()}")
                finally:
                    if capsys:
                        capsys.stop()
                    if monkeypatch:
                        monkeypatch.undo()
                    for d in tmp_dirs:
                        import shutil

                        shutil.rmtree(d, ignore_errors=True)

    for f in failures:
        print("\n" + f)
    total = passed + failed + skipped
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped ({total} total)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
