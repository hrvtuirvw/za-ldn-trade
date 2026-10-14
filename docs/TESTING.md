# Verification boundaries / 検証情報

[English home](../README.md) · [日本語ホーム](../README_JA.md)

## Software and platform coverage / ソフトウェアの検証範囲

| Area / 対象 | Evidence / 根拠 | Limit / 制約 |
|---|---|---|
| PA9 tools | Synthetic regressions, public-data calculations, installed CLI/GUI | Not a complete legality checker or proof that generated Pokémon load in-game |
| Packaging | Allowlisted source ZIP, wheel RECORD/data/licenses, normalized sdist, fresh installation | Python is required; GUI also needs Tk; not a standalone executable |
| Linux GUI | Mapped X11/Tk, Japanese/English, small/high-DPI layouts, normal exit and same-process reopen | Local visual review: Linux x86-64 / Python 3.14.8; native CI coverage is listed separately below |
| Optional BSS helper | Bundled C source, matching-kernel Kbuild, synthetic ownership/cleanup tests | Compilation does not prove module loading, RF behavior or a physical trade |
| Current Direct/Relay/helper paths | Synthetic protocol, failure-path and packaging checks | No new physical exchange/save/recovery certification |
| Native Windows / Ubuntu CI | Python 3.10/3.12/3.14 regressions, artifact gates and exact installed CLI/GUI; [successful fix baseline](https://github.com/hrvtuirvw/za-ldn-trade/actions/runs/38583042096) | Hosted CI, not arbitrary desktops, WSL or physical radio/trade verification |

**Tests passing does not prove that all bugs are absent.** Report skips and subtests separately. Normal/development runs of the same population are not independent cases. A synthetic adapter or peer is not hardware evidence.

**自動テスト合格は全バグ不存在・実機成功の証明ではありません。** 同じ試験の通常/dev実行を二重計上せず、skip・subtest・合成backendを区別してください。

## Historical hardware evidence / 過去の実機証拠

These observations apply only to the configurations named below, not automatically to a newer package.

- **Development setup:** Z-A 2.0.2 / AWUS036ACH / `rtw88_8812au` / Linux 7.2.8-1-cachyos / additional BSS helper and dedicated recovery supervisor. Physical trade, save synchronization, durable PC receipt and radio restoration were verified; the operator confirmed retention after restarting the game.
- **Unmodified 0.8.6 wheel, 2026-10-10:** the same adapter/driver, Linux 7.2.9-1-cachyos / Python 3.14.7, direct single-interface mode **without BSS seeding**. One LDN/Net/Session/RTT handshake and normal shutdown succeeded. A separate supervisor verified the original radio state for at least 30 seconds before/after, final equality and ownership release. **No trade/save commands were sent.** An earlier RuntimeError did not recur; its cause is still unknown.
- Other adapters have no Z-A validation by this project. [Japanese hardware matrix](WIRELESS_COMPATIBILITY.md) · [English hardware matrix](WIRELESS_COMPATIBILITY_EN.md).

開発構成の交換成功と0.8.6の補助なし接続成功は別の証拠です。後者は交換・保存を行っておらず、新しい配布版、Windows/WSL、Relayの実証へ転用しません。

## Regression contracts / 回帰テストで守るもの

The portable suite uses synthetic files/peers and genuine crypto/compression, not consoles, real keys or personal saves. It covers:

- **Input/storage:** size and JSON-depth limits, exact ZSTD framing, malformed endpoints, FIFO/symlink substitution, concurrent changes, no-clobber publication, durability and recovery artifacts.
- **Workers/results:** constructor/start failure rollback, descriptor cleanup, cancellation within capture batches and blank/metadata reads, retained prompts/results through display faults, exactly-once acknowledgement and no premature close.
- **Protocol/model:** standard-handshake wire references, non-generic outbox rejection, refusal of completion before FinalAgreement transmission, PA9 calculations and serialization.
- **Inherited/shared code:** all four local entrypoints supply the same explicit Z-A profile without starting a radio; nondefault request fields, independent room matchers, helper opt-in and historical aliases are preserved. Legacy replay closes its input on success/error. Relay JSON scanning shares the common implementation but keeps its own 32-container cap.
- **Usability:** single editor viewport, persistent compact section/file controls, current-selection catalog paging through all results, no spurious edit on unchanged selection, corrected-input trace ownership, editor-local Ctrl+Tab, offline-first startup, fixed new/existing Save, bilingual name/number search, explicit row selection, internal/National species mapping, retained forms/unknown IDs, native-font picker rows, compact task selection, menu disclosure, input-error focus, explicit trainer-ID generation and picker cancellation/ownership.
- **UI navigation/forms:** persistent task/start controls, modal-aware keyboard switching, empty/save-action states, local bilingual species filtering without implicit selection, retained optional input, read-only result scrolling and convergent nested layout at small/high-DPI sizes.
- **Presentation:** concrete-record cache provenance and invalidation, subclass replacement, partial bulk-edit failure, bilingual input preservation, owned-trace disposal, foreign-interpreter isolation and normal/direct-destruction lifetime checks without harness pre-cleanup; new-PA9 Create/Cancel/window-close/root-destroy trace cleanup while preserving foreign traces.
- **Logs/performance:** ordered payload/drop notices, escaping/truncation, multiline and language-rebuild retention, 2,000-message queue and 5,000-line display limits, line/character/time-sliced rendering, Stop/Close while a synthetic worker has a backlog.
- **Distribution:** explicit member set, legacy-module exclusions, archive type/expansion limits, input identity, existing-venv setup, installed entrypoints, translation coverage, packaged Markdown link/anchor checks and executable offline documentation examples.

Deterministic work-count assertions are separate from timing observations. GUI log rendering uses an 8 ms cooperative preparation budget, at most 64 lines and a 32 Ki-character threshold; a final escaped line can cross the threshold. Individual Tcl calls and splitting one message are not preemptible. File I/O blocked inside the OS is also not instantly cancellable. Neither synthetic timings nor interpreter adapters prove radio speed or execution on another Python version.

The cache is only for presentation; Apply/save validation remains independent. Lifetime checks concern same-process reopening, not memory persisting after OS process exit. [Change history](../CHANGELOG.md) records when individual fixes were introduced.

## Before calling a configuration physically verified

Record app version, OS/kernel, Python, adapter/revision, driver/firmware, helper use, Direct/Relay, interface mode and channel. Do not publish serials, MACs, keys, private room codes, received Pokémon or raw captures.

1. Confirm current device enumeration, a usable PHY and dedicated ownership/restoration baseline.
2. Verify bounded discovery, action-frame I/O, association, LDN authentication and IP assignment.
3. Verify the full Pia handshake before separately authorized individual/commit traffic.
4. Verify both confirmations, native save synchronization, durable PC receipt and retention after game restart.
5. Verify normal/cancelled cleanup: no remaining owned module, worker or lease, and original radio state restored.

**USB列挙、PHY、接続、交換、保存、復旧は別の段階です。** 過去の状態ではなく、その構成の現物と各段階の証拠を確認してください。

## Continuous integration

The native Linux/Windows matrix targets Python 3.10/3.12/3.14. The [CI repair baseline](https://github.com/hrvtuirvw/za-ldn-trade/actions/runs/38583042096) passed all six jobs; version-tag release runs repeat these gates for their own exact source. Linux includes Trio; supported Linux/Python 3.12+ jobs also import the pinned live dependencies. Device I/O is synthetic: CI must not load modules, connect to a console or reset USB controllers.

Jobs build from the public export, check archives and smoke-test installed CLI/GUI entrypoints outside source. Matching version tags repeat the gates before creating a draft prerelease. See [release procedure](RELEASING.md).

`VERIFICATION.json` from `check_za_release.py` certifies **artifact integrity**, not arbitrary-platform or hardware execution. Inspect runtime/CI logs for those claims.

`MANIFEST.json` is an export-time inventory. Its execution-verification flags are not updated by CI; use the commit/tag-bound CI run for native execution evidence. The exporter and artifact checker themselves do not perform hardware or arbitrary-platform validation.
