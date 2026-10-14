# Changelog

## 0.9.2 — native Windows/Linux compatibility fixes

- Correct Windows path/descriptor metadata admission without weakening same-API change detection, content identity, link/type/size checks or descriptor cleanup.
- Refresh new-PA9 dialog scroll extents after disclosure and resizing; retain access to controls on small/high-DPI windows. Avoid expensive wrapped-index layout when following long GUI logs on Windows.
- Preserve hash-checked catalog bytes across Git checkouts. Reject original unsafe ZIP member names before host-specific normalization.
- Verify native Windows and Ubuntu on Python 3.10/3.12/3.14, finalized distribution formats and exact installed CLI/GUI. Make GUI fixtures, native menus and durability fault injection platform-correct without skipping failures or weakening assertions.
- Preserve protocol identities, communication, durable-save/cancellation behavior, existing settings and upstream credits. No new radio/hardware validation or runtime dependency.

## 0.9.1 — za-ldn-trade

- Rename the application, CLI display, distribution and installed commands to `za-ldn-trade`; use `za-ldn-trade-gui` for the installed GUI and `Start-za-ldn-trade.cmd` for the Windows source launcher. Source module names remain unchanged.
- Preserve the existing preference directory, protocol nickname/identities, lock paths and all trade/save/cancellation behavior. No data migration or new runtime dependency.
- Adopt the supplied trade-first Japanese README and matching English text, refresh synthetic screenshots and retain explicit upstream attribution and component licenses.

## 0.9.0 — Z-A-only communication namespace

- Move required LDN/Pia transport, nickname validation and optional helper ownership to `zasim.ldn`. The public source ZIP, wheel and sdist no longer contain the historical `frlgsim` package or an import shim. This intentionally breaks the old Python import path; CLI/GUI entrypoints are unchanged.
- Remove the old-title replay/beacon implementation and constructor defaults from the public package. Require explicit profile/identity and validated Update sequence/generation in low-level Pia builders; Z-A adapters retain their existing wire and admission behavior.
- Keep archival development consumers intact and move the two replay-lifetime scenarios to the non-distributed compatibility suite. Preserve the union of existing scenario IDs, with dedicated namespace/import-denial and reintroduction tests.
- Preserve communication, durable saving, cancellation/recovery and component attribution. No runtime dependency addition, real-radio operation or publication.

## 0.8.17 — fixed editor navigation and complete catalog browsing

- Give the editor a single form viewport, with document actions and section navigation outside it. On compact windows, use a persistent section selector and File menu; keep New/Open accessible in the empty state. Ctrl+Tab switches editor sections without switching tasks.
- Browse every name-catalog result in bounded pages, reopen at the current selection, and horizontally scroll long names. Page/search changes clear stale selections; choosing an unchanged value does not create a spurious pending edit.
- Clear error decoration when a creation field is corrected, using only dialog-owned variable traces. Match kana/width variants in creation search and preserve cancellation and input validation.
- Keep radio/controller/protocol/model/durable-save implementations and runtime dependencies unchanged. No automatic device operation or publication.

## 0.8.16 — name-first editing and simpler everyday actions

- Open directly in the offline PA9 editor. One persistent Save action handles both new and existing documents, including pending input; Save as remains available with Ctrl+Shift+S.
- Search/select species and forms, moves, held items and abilities by Japanese/English name or number, using attributed pinned PKHeX display data. Unknown numeric values remain editable; names do not certify legality or availability.
- Prioritize species, nickname, level, item, ability, nature and gender. Move internal values and file details behind an explicit switch; put optional actions in a compact menu and use a task selector instead of wrapped navigation on small/high-DPI screens.
- Add readable creation choices and an explicit new-trainer-ID button. Invalid numeric inputs reveal/focus their field; a mismatched name/ID pair is rejected instead of silently reinterpreted. Picker cancellation and language changes preserve the document and pending input.
- Size picker rows from the native font, retain modal/closing guards and release owned picker traces. Protocol, radio, core creation/calculation and durable-file-save implementations are unchanged.

## 0.8.15 — inherited-code and shared-boundary cleanup

- Audit the original FRLG tree against runtime callers and all three distribution formats. Keep shared LDN/Pia and historical import APIs; the archived FRLG/RFU engines remain excluded.
- Centralize Z-A's local-transport profile across trade, capture, handshake and relay, preserving room matching, title/scene selection, packet limits, helper opt-in and constructor injection.
- Reuse the common JSON-depth scanner with the relay's existing 32-container cap and error interface. Remove unread cross-session counters and a redundant post-validation nickname check, not per-session statistics or validation boundaries.
- Close legacy replay input deterministically on success and parsing failure. Clarify shared versus title-specific source ownership and remove stale FRLG-only/untested-offline descriptions.
- Add synthetic constructor-equivalence, profile isolation, nesting, resource-lifetime and retained-API regressions. No new runtime dependency, radio experiment or publication.

## 0.8.14 — task-focused desktop UI

- Keep task navigation, language/help and trade/capture start controls outside scrolling forms. Add modal-aware Alt+1–5 task shortcuts; preserve the original task order and operation callbacks.
- Refresh stock ttk colors, spacing, focus and primary-action hierarchy without new dependencies or reduced OS text scaling. Show a useful empty editor instead of a wall of disabled fields; emphasize Save as for new documents and Save for existing files.
- Reflow editor fields and stat controls using intrinsic widths, avoiding parent/child breakpoint feedback. Keep horizontal fallback for genuinely wide tables and read-only, horizontally scrollable inspection results.
- Group new-PA9 fields, add local species name/number filtering and disclose optional fields without discarding them. Filtering never silently changes the selected species; dispose both dialog-owned traces.
- Preserve translation values for retained messages, unapplied input, dirty/save prompts, result retention, cancellation timing and all radio/protocol/persistence code. Refresh synthetic screenshots and add UI/keyboard/search/lifetime/geometry regressions.

## 0.8.13 — first-run polish and new-dialog cleanup

- Reorganize Japanese/English README and user guides around installation, the first offline PA9, then optional wireless setup. Clarify Apply versus Save, formats, recalculation and verification limits.
- Use explicit virtual-environment interpreters in source examples and isolated wheel installation instructions; keep advanced radio preparation separate from normal-user GUI use.
- Refresh troubleshooting, contributor setup and the legacy GUI-document pointer. Add checks for packaged documentation links, command examples and bilingual first-run paths.
- Release the new-PA9 dialog's owned encounter-variable trace on Create, Cancel, window close and root destruction. The README walkthrough exposed retained dialog/app/root references in same-process reuse; this is not data loss or memory surviving process exit. Preserve foreign traces and active/rejected creation behavior.
- Runtime changes are limited to new-dialog lifecycle cleanup. Translations, protocol, saving, worker control and cancellation timing are unchanged.

## 0.8.12 — unused text and import cleanup

- Remove seven orphaned English translations from the retired connection-selector UI. Preserve all active messages and their placeholders.
- Remove unused implementation/test imports and two unread test-local assignments; retain fixture registrations, shared compatibility names and call side effects.
- Consolidate repeated release/hardware history through the changelog and verification guide. Remove stale current-version labels and keep bilingual setup, security, licensing and evidence limits.
- Add translation-liveness and evergreen-guide regressions. No protocol, serialization, validation, save, cancellation or restoration behavior changes.

## 0.8.11 — GUI callback lifetime and responsive log presentation

- Dispose the editor's owned variable traces on normal Exit and direct root destruction. Preserve foreign traces and other Tk interpreters. This fixes retained app/root references during same-process reopen, not an OS-process-exit leak.
- Present controller logs in ordered, bounded line/character slices with a cooperative time budget and one Text insertion/scroll per slice. Yield to Tk between slices while preserving escaping, line truncation, queue-drop notices and the displayed tail limit.
- Retain pending prompts/results, single acknowledgement and presentation retry through log-widget failures. Stop/Close remain available while draining a large log backlog. No worker daemonization, radio/transaction/timing changes or idle-poll retuning.
- Add mapped close/reopen, foreign-interpreter, sliced backlog, multiline/locale, fault/retry and ordering regressions; retain previous scenario IDs and distribution exclusions.

## 0.8.10 — presentation-cache provenance

- Keep the 0.8.8 payload cleanup and 0.8.9 redundant-work reductions. Do not retain subclass-derived validation results in the concrete PA9 presentation cache: loading a plain record with identical bytes must recompute its own errors, warnings and checksum status.
- Add bilingual regressions for both false warnings and hidden errors across subclass-to-concrete document replacement. Repeated pending edits on the resulting concrete record remain cached; Apply/save validation and all transport/cancel/recovery code are unchanged.

## 0.8.9 — standard-path simplification and redundant-work removal

- Remove twelve research-only handshake keyword options, their packet mutation/oracle/sweep branches, helper functions and research-only report fields. The public CLI keeps its standard connection options. Preserve standard authenticated wire bytes and core milestones with frozen synthetic references.
- Reject an injected trade engine claiming completion before the actual local FinalAgreement send boundary. Remove the dummy-completion fallback and unused commitment timer; move the pumping fixture onto the normal outbound contract. This is a synthetic state-contract correction, not a reproduced physical-console bug.
- Allocate capture Reliable reassemblers only on a new stream; read a PA9 species/form identity once per personal-data search instead of on every table row.
- Reuse GUI presentation validation only for unchanged concrete PA9 bytes; always revalidate subclasses with potentially mutable properties. Pending form input never substitutes for applied record validation. Do not rescan EV fields for unrelated edits; refresh bulk IV/EV changes once, including partial failure recovery.
- Preserve apply/save/SyncSave/cancel/recovery checks, component licenses, shared FRLG consumers, public regression scenarios and prior candidates. A prior synthetic zero-send completion test now requires rejection and no saved file.

## 0.8.8 — public distribution cleanup

- Exclude 15 legacy FRLG modules, exception_metadata and the standalone rejection probe from the Z-A public source ZIP, wheel and sdist. Keep the six shared frlgsim files, licenses and public tests/docs/CI. Historical development files and previous candidates are not deleted.
- Use the source-export module allowlist for setuptools wheel selection; narrow shared-package declarations and sdist inputs.
- Move the fixed synthetic UserData generator into public test fixtures. Remove an unbound old editor-wheel handler and test the active route instead. Remove three unreferenced private helpers and unused local computations.
- Preserve public aliases and callable research-handshake options. Preserve all completion, SyncSave, recovery, cancellation and save guards; characterize the difference between real-engine and injected synthetic completion contracts. This cleanup is not a reproduced live defect or a new physical exchange certification.

## 0.8.7 — public input, lifecycle and release-boundary debugging

- Strictly validate one complete ZSTD frame, rejecting trailing/concatenated data and truncated empty frames while bounding decompressed output. Valid packet encoding and handshake negotiation are unchanged.
- Close relay sockets when accepted-socket setup or bound-address queries fail before protocol startup.
- Check capture cancellation and duration within receive batches, not only between batches. Keep GUI Stop usable even if the operation-start log fails. Do not claim a capture file exists when cancellation happened before recording started.
- Pin public-source file identities through nonblocking reads; reject swapped links/special inputs. Bound both compressed and expanded archive input, including TAR metadata; reject nonregular wheel members and noncanonical names before normalization.
- Validate both distribution artifacts before normalizing either; remove owned staging files on failure. This is not a multi-file filesystem transaction.
- Add public synthetic handshake/crypto/failure tests and existing synthetic capture tests to the portable suite; fix the documented handshake capture suffix.
- Record the separately completed 0.8.6 helper-free physical handshake/normal shutdown/restoration. That connection-only observation does not certify physical trade/save or this 0.8.7 candidate.

## 0.8.6 — repeated fault-injection debugging

- Reject a capture overwrite target changed into a FIFO without blocking; malformed endpoint types become per-packet analysis errors instead of aborting the capture.
- Preserve queued live and offline results across presentation failures. A broken log widget cannot suppress terminal dialogs or stop the event pump; reject invalid offers even if their error dialog fails.
- Roll back GUI worker construction failures as well as thread-start failures, and display concurrent-file-change errors during PA9 inspection.
- Cancel native ttk and Python timers safely before destruction, including Tcl list-valued progress-bar timer scripts.
- Validate the actual existing `.venv-za` interpreter before setup changes dependencies; selecting a newer bootstrap Python does not upgrade an old venv.
- Add synthetic fault/positive controls. No protocol, PA9 format, successful helper C source, or new hardware/Windows execution claim is changed.

## 0.8.5 — Public-release polish

- Fix helper timeout traceback disclosure and malformed empty BSS-cache records; preserve the successful helper C source unchanged.
- Make the helper setup accept PATH-based Python selection and a visible output folder; hide irrelevant interface settings during reuse.
- Add an exact ZIP/wheel/sdist integrity gate and installed CLI/GUI smoke; include existing synthetic radio regressions and explicit live test dependencies in CI.
- Version tags now produce a draft prerelease after the full portable matrix and final-asset checks, rather than immediately publishing an experimental stable release.
- Consolidate bilingual setup/support guidance around the AWUS036ACH reference configuration, document evidence limits and troubleshooting, and distinguish component licenses in package metadata.
- New packaged wireless integration still needs physical trade/save/recovery validation; Windows/WSL execution remains separately qualified.

## 0.8.4 — Optional AWUS036ACH BSS helper

- Include the unchanged successful-development C helper source and local running-kernel build/check commands. No automatic system installation, module load or driver replacement.
- Opt-in GUI directory chooser and direct/relay-server CLI integration, exact scan-event/cache checks, exclusive ownership and explicit cleanup failures. Normal path stays the default.
- New integrated path is not physically reverified; Linux matching headers/build tools and permitted module loading are required.

## 0.8.3 — Selection-first wireless UI

- Select a local Linux adapter from a read-only list with product/driver/interface and use/disabled status. No ordinary PHY/interface-name entry; advanced settings are collapsed by default.
- Plain-language **This PC's Wi-Fi / Use a prepared connection** replaces the backend jargon. Linux initially shows its local picker; Windows retains the prepared-connection path.
- Bind selection to sysfs object identity, preserve it across reorder/language changes, invalidate it on replacement, and never silently select a different adapter.
- Run a fresh, cancellable read-only prerequisite/usage/identity check in the existing worker before transport/key access. A passing check is still not physical wireless certification.
- Privilege escalation, driver installation, remote-radio enumeration and automatic Relay startup are not added. Windows/WSL backend preparation remains separate.

## 0.8.2 — fault-injection debugging and input/lifecycle hardening

- Pin regular-file descriptors and use non-blocking/no-follow opens where available, preventing FIFO/path-swap hangs in PA9 identity/loading and capture input.
- Bound PA9 inspection, offline command inputs, preferences and identity reads at both metadata and read boundaries.
- Apply a deterministic 64-container JSON nesting limit to captures/preferences; malformed session metadata produces a validation error, not a CLI traceback. Preserve invalid language settings and fall back to the OS language on startup.
- Reject stale/reentrant operation starts until previous results are handled; preserve the active result kind and recover GUI controls if an analysis or diagnostic thread cannot start.
- Cancel owned Tk timers before exit and on root destruction, preventing callbacks into deleted widgets on subsequent launches.
- Add seeded malformed decoder inputs, reordered/duplicated frames, genuine GCM tamper and Zstandard-size tests, save-sync failure gates, FIFO substitutions and per-fsync transaction fault injection.
- No protocol/state-machine or PA9 model changes; no new hardware or Windows/WSL execution claims.

## 0.8.1 — reviewed reliability and data-preservation fixes

- Accept buffered retransmissions whose ACK watermark changes without accepting changed payload, flags, or destination bitmap; deliver assembled data once.
- Share PA9 editor/trade filesystem safeguards: path-scoped process locks, atomic no-clobber creation, approved-content rechecks, verified publication and durable recovery copies.
- Keep unacknowledged terminal results until the GUI presents them, including uncertain-trade warnings during exit and retries after native-dialog failure.
- Observe optional capture cancellation before/after each bounded input read, including blank lines and skipped metadata.
- Refresh editor identity after unlinking the temporary hard link so Save As followed by in-place Save does not falsely report an external modification.
- Add unchanged external-review regressions, multi-process collisions, sequence wrap/reordering, actual-Tk exit-order tests and skipped-input cancellation tests.
- No new hardware, Windows/WSL, or game-legality claims. Existing-file replacement is not a filesystem compare-and-swap against non-cooperating applications.

## 0.8.0 — bilingual desktop and public-release polish

- Japanese/English language selector, OS-aware first launch, saved preference and explicit CLI override.
- Preserve unapplied editor input, document identity, selected tabs and logs during idle language switches; block switching during jobs/modal dialogs.
- Translate controls, dialogs, status, nature/gender names and read-only environment reports; package a validated English catalog.
- Focus offline tabs by hiding wireless settings; improve visual hierarchy, primary actions, wrapped editor summaries and keyboard navigation.
- English landing page, paired Japanese documentation, English setup/wireless/creation guides and bilingual contribution templates.
- Add small/high-DPI bilingual interaction and catalog/format/privacy regressions.
- No new hardware, game-loading, legality, Windows or WSL validation claim; no protocol or save-format changes.

## 0.7.1 — explicit wireless prerequisites and evidence boundaries

- Add opt-in read-only `doctor --radio --phy NAME`: selected-Wiphy modes, CCMP, 1/6/11 channels, both distinct control-port capabilities, busy routes/IP/carrier and rfkill.
- Query the custom EtherType flag via bounded GET_WIPHY; `iw` does not print it. Unknown/ambiguous inspection is not a pass.
- Add scrollable asynchronous GUI diagnostic and share the same prerequisite check with the WSL launcher. No automatic driver/radio changes.
- Publish a source-attributed adapter table separating upstream FRLG reports, specialized Z-A lab success, and unverified standard-backend operation. No purchase guarantee or new physical exchange claim.

## 0.7.0 — save-independent PA9 creation

- Add GUI/CLI creation using bundled, attributed public ZA personal/growth/move data.
- Six player starter gift presets and explicit custom-encounter drafts for 490 species/forms.
- Calculate real level/party stats (mint, IV/EV, Hyper Training), PP and Plus flags.
- New documents auto-calculate; explicit recalculation and stored-to-party conversion for existing records.
- Preserve existing GUI layout, atomic saves and no-clobber protections.
- Separate structural/model/sample legality evidence from untested game loading and physical trades.
- No runtime SDK, Wine, save, ROM, keys or private-work dependency. No publication implied.

## 0.6.0

- Full 10-protocol Pia registry and independent BroadcastReliable acknowledgement.
- Native UserData/peer synchronization before the trade UI.
- Preview-first selection ordering to avoid clearing a prematurely committed offer.
- Native SyncSave barriers and durable received-record verification; FinalAgreement alone is no longer a user-facing success.
- GUI/CLI enable native peer/save synchronization by default; 300-second user-operation timeout.
- Independent PC display profile; no copying of the console's trainer profile.
- Read-only environment checks, in-app help, non-colliding received-file suggestions, visible stop/exit controls.
- Portable source archive, wheel/sdist metadata, Windows/Linux setup and CI.
- Pre-publication GUI layout fix: responsive connection fields, scrollable forms/help, keyboard focus reveal, horizontal editor fallback, screen-bounded initial size and always-visible stop/exit. Mapped Tk regression coverage at 96/144/192 DPI and small windows; no radio/engine behavior changes.

The engine was tested in a specialized hardware lab path. This does not certify every Wi-Fi adapter, driver, kernel, or the public standard relay path. Experimental lab modules and private captures are not distributed.
