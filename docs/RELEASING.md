# Releasing / 公開前チェック

This is a **maintainer procedure**, not permission to publish to the upstream repository. A prepared candidate and an actual GitHub publication are separate outcomes.

## 1. Establish the public source boundary

Never push a dirty development workspace containing private work/history. Confirm the intended repository owner/destination first; an existing `origin` may belong to upstream.

```sh
python -B scripts/export_za_source.py --output /new/public-source-directory
```

The destination must be new. Export copies only the allowlisted source, tests, package data, licenses and documentation, not `.git`, developer environments, captures or private work. The module allowlist is also used by setuptools wheel selection. Communication modules live under `zasim.ldn`; archived code and development-only compatibility tests are excluded from all three formats. There is no legacy import shim. Public test fixtures stay in source ZIP/sdist, not wheel. Do not remove callbacks or completion/save guards based only on unused-name reports. Review that export and its `MANIFEST.json`; initialize/version-control it in the intended public repository separately. Preserve the old release rather than overwriting it.

公開先と権限を確認し、正規workspace全体ではなく**確認した公開export**を使用します。個人データや旧解析historyを含むdirty treeをそのままpushしません。

## Naming (0.9.1)

The application and Python distribution are `za-ldn-trade`. Installed commands
are `za-ldn-trade` and `za-ldn-trade-gui`; the Windows source launcher is
`Start-za-ldn-trade.cmd`. Source modules retain the `zatrade` and `zasim` names.
Existing preference/lock paths and protocol identities have not moved.

## Python API migration (0.9.0)

The public `frlgsim.*` import paths are intentionally retired. Their required
communication modules are now `zasim.ldn.{transport,pia_connect,ldn_single,ldn_name,bss_seed}`.
There is no shipped replay class, old-title beacon decoder or old builder profile.
Use the Z-A entrypoints or supply an explicit profile to low-level builders.
Validated Session Update values must be acknowledged through the Z-A adapter.
That namespace migration did not rename CLI/GUI commands or change on-wire
behavior. The subsequent 0.9.1 display/command rename is described above.

The developer archive keeps legacy consumers and their replay regression cases;
those files must not enter public source or runtime artifacts. The release check
compares the union of public and non-distributed compatibility scenario IDs with
the previous baseline, not merely a lower public test count.

## 2. Test and build the candidate

Use a dedicated build environment in the exported tree. In a POSIX shell, create and activate it before the commands below:

```sh
python3 -m venv .venv-build
. .venv-build/bin/activate
```

Windows users can create it with `py -3 -m venv .venv-build` and replace each `python` below with `.\.venv-build\Scripts\python.exe`; no activation-policy change is required.

From the reviewed export in a fresh Python environment:

```sh
python -m pip install '.[test]' build
# Linux + Python 3.12+: also exercise the optional dependency path.
python -m pip install '.[live]'
# Linux GUI tests need a desktop or an owned display:
xvfb-run -a python -m pytest -q -rs -W error::ResourceWarning
python -m build --outdir /new/artifact-directory
python -B scripts/normalize_za_artifacts.py --directory /new/artifact-directory
python -B scripts/build_za_release.py --output /new/artifact-directory
python -B scripts/check_za_release.py --directory /new/artifact-directory \
  --write-checksums --report /new/artifact-directory/VERIFICATION.json
```

On Windows, omit the Linux live installation and `xvfb-run`; run `python -m pytest` in its ordinary desktop environment. The version is declared in `zasim/__init__.py`. Normalization preserves payloads while removing build-owner/time metadata; it is not a proof of independently reproducible wheel compilation.

The release gate checks the exact source ZIP, all wheel source/data/license bytes, wheel RECORD, entrypoints, Python/version/license metadata, sdist members and deterministic metadata. It rejects extra/missing members, corrupt checksums and existing report/checksum outputs. It does not load modules or test hardware.

## 3. Test the exact finalized wheel outside source

Install the finalized `.whl` in a fresh venv. Copy `scripts/smoke_installed.py` outside the source tree, change to that separate directory, and run it with the installed environment's interpreter:

```sh
/path/to/fresh/venv/bin/python -I -B smoke_installed.py \
  --expected-version VERSION --gui
```

Linux headless checks can wrap that command with `xvfb-run -a`. This exercises the installed CLI, synthetic PA9 creation/inspection, helper command availability, the registered GUI entrypoint, language switching and normal exit. Keep exact installed-package checks separate from tests importing a source checkout. Verify a fresh source-ZIP setup too.

For the optional kernel helper, compile the packaged source with matching headers on the supported host; **compilation does not imply successful module loading or trading**. Physical validation follows [the evidence checklist](TESTING.md), with fresh device identity, exclusive ownership and verified restoration. Never run radio tests inside generic CI.

## 4. Review wording and assets

- Read the Japanese and English setup/compatibility guides. AWUS036ACH historical success, other unverified cards and this exact distribution are separate claims.
- Check screenshots use synthetic data and reflect the shipped UI.
- Confirm no keys, ROMs, saves, real Pokémon, unredacted logs, private paths, compiled `.ko` or unrelated executables are included.
- Preserve upstream AGPL notices and the separate GPL2 helper/GPL3-derived-data notices. Package license metadata identifies separate components; it does not relicense them.
- Review `RELEASE_NOTES.md` for the current version, limitations and actual platform results. Do not call authored CI a passed CI run.
- Read `SHA256SUMS` and run the integrity checker again after asset copies. Do not rebuild/normalize after certifying a different wheel.

## 5. GitHub: draft first

Only after publication is authorized for the intended repository, push the reviewed source and a matching `vVERSION` tag. The tag workflow reuses the Linux/Windows/Python matrix. If it passes, a Linux packaging job builds and audits the final bytes, performs another exact-wheel smoke and creates a **draft prerelease** with source ZIP, wheel, sdist, checksums and an integrity report.

The workflow has read-only permissions except the final packaging job's release-write permission. It does not upload to PyPI or automatically turn experimental software into a public stable release. If a maintainer has already created the release from locally verified assets, CI still runs its checks but leaves those published assets untouched. Review the draft, assets and CI logs before the maintainer explicitly publishes it.

**この作業手順を整えたことと、GitHubへ公開済みであることは別です。** 最終公開はmaintainerが公開先・検証結果・実証範囲を確認して行ってください。
