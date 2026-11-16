# Releasing

A release is a tag. Pushing `vX.Y.Z` runs `.github/workflows/release.yml`, which checks
everything it can, then **waits for a person to approve the upload**. PyPI will never let a
version number be used twice, even after a release is deleted, so the checks come first and
the approval is the last step.

## One-time setup (cannot be done from this repository)

1. **PyPI:** create the project (or a *pending publisher* for a name that does not exist yet)
   and add a trusted publisher: owner `euphoria95`, repository `OnionAccelerator`, workflow
   `release.yml`, environment `pypi`. Do the same on **TestPyPI** with environment `testpypi`.
   There is no API token anywhere in this setup, by design.
2. **GitHub:** Settings → Environments → create `pypi` and `testpypi`. On `pypi`, add
   yourself as a *required reviewer*, and restrict it to `v*` tags. That is what makes the
   upload wait for you.
3. Both `onionaccelerator` and `rvtree` were free on PyPI on 2026-11-16. Only
   `onionaccelerator` is published: `rvtree` stays bundled inside it. `remote_viewer/` still
   builds on its own; publishing it separately is a decision to make when somebody needs to
   depend on it without this program, because it creates a second version number to keep
   honest.

## Before tagging

- [ ] Every PR for the release is merged and **CI is green on `main`**.
- [ ] **The security audit's High findings are fixed.** They are the reason not to publish
      yet: a published version is a version people run. (See the maintainer's audit notes.)
- [ ] `docker build .` succeeds. The image is built by nothing in CI.
- [ ] `tests/test_farm_real_tor.py` and `bench/onion.py` have been run once against real Tor
      (`pytest -m network`); everything else is tested against a stub, and these are not.
- [ ] On a quiet machine, `python -m bench --check` passes. If the numbers moved for a reason
      you understand, `python -m bench --write-docs` and commit the page. It takes about
      fifteen minutes and is not run in CI because timing is not CI's to judge.
- [ ] `pyproject.toml`: `version = "X.Y.Z"` (drop `.dev0`).
- [ ] `CHANGELOG.md`: rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`, add a fresh
      empty `## [Unreleased]` above it, and update the link references at the bottom. The
      workflow refuses a tag whose version has no dated heading.
- [ ] `README.md`: delete the sentence that begins "Until the first release is on PyPI".
- [ ] Commit as `Release X.Y.Z`.

## Rehearse

Run the workflow by hand against TestPyPI: Actions → Release → *Run workflow* → `testpypi`.
Then, in a clean directory:

```bash
pipx install --index-url https://test.pypi.org/simple/ \
    --pip-args "--extra-index-url https://pypi.org/simple/" onionaccelerator==X.Y.Z
onionaccelerator --full-help | head
```

(TestPyPI does not carry the dependencies, hence the second index.)

## Release

```bash
git tag -s vX.Y.Z -m "OnionAccelerator X.Y.Z"      # -a if you do not sign
git push origin vX.Y.Z
```

The workflow builds the sdist and the wheel, checks the tag against the version and the
changelog, runs `twine check --strict`, installs each distribution into a clean virtualenv
and runs it from outside the checkout, then stops at the `pypi` environment. Approve it there.

## After

- [ ] In a clean environment: `pipx install onionaccelerator==X.Y.Z` and
      `onionaccelerator --full-help`.
- [ ] Create the GitHub release from the tag; paste the version's changelog section.
- [ ] Bump `pyproject.toml` to the next `.dev0`.

## Versioning

`0.N.P` until the command line and the index schema settle. In 0.x, a minor release may
change a flag, a default or the schema, and the changelog says so under **Changed**. The
index migrates its own schema forward and refuses a file written by a newer version, so
going back to an older release never silently corrupts an index. 1.0 is when the flags and
the schema are a promise.
