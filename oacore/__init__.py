"""OnionAccelerator's own modules, split out of the OnionAccelerator.py script.

OnionAccelerator.py stays the entry point and the public surface: it re-exports what
lives here, so `import OnionAccelerator as oa; oa.download_file(...)` and every
monkeypatch the tests apply through `oa` keep working.
"""
