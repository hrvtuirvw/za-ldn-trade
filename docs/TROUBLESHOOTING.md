# Troubleshooting / 困ったとき

[English user guide](USER_GUIDE_EN.md) · [日本語の使い方](USER_GUIDE.md) · [Verification / 検証情報](TESTING.md)

Start with the symptom you can observe. Do not change drivers or wireless settings to fix an offline editor problem.

まず見えている症状から切り分けます。オフライン編集の起動失敗を、無線設定やドライバ変更で直そうとしないでください。

| Symptom / 症状 | First check / 最初に見るところ |
|---|---|
| Window does not open / 起動しない | Python, the prepared venv, Tk and desktop display / Python・venv・Tk・画面 |
| Missing module / 依存が見つからない | Run the interpreter that setup prepared / setupで準備したPythonを使う |
| Edits are not in the file / 編集が保存されない | Apply versus Save; actual output path / 適用と保存の違い・保存先 |
| Adapter is missing / 無線機がない | Correct Linux host → USB → driver/PHY / 調べるホスト・USB・driver/PHY |
| Helper will not build/load / 補助の失敗 | Running kernel, matching headers and ownership / 実行カーネル・ヘッダー・所有権 |
| Uncertain trade / 交換結果が未確定 | **Do not retry; inspect preserved data / 再交換せず保全データを確認** |

## Wrong Python or missing modules / 別のPython・依存不足

Run from the extracted source folder with the prepared interpreter. Setup does not activate it in your shell. Do not use `sudo pip` or install into the system Python to hide an environment mismatch.

展開フォルダーで、準備済みのPythonを明示します。setupはシェルのPythonを切り替えません。環境違いを隠すために`sudo pip`やシステムPythonへの導入はしないでください。

```sh
.venv-za/bin/python zatrade.py --version
.venv-za/bin/python -m pip check
.venv-za/bin/python zatrade.py doctor
```

On Windows, replace `.venv-za/bin/python` with `.\.venv-za\Scripts\python.exe`. A wheel uses that environment's `zatrade`, not a missing `zatrade.py` file. See [command equivalents](USER_GUIDE_EN.md#which-python-to-use-for-cli-commands) / [コマンド対応](USER_GUIDE.md#cliで使うpython).

`doctor` checks prerequisites, not a trade. No radio/live dependencies are needed just to edit a PA9. Linux `--live` preparation needs Python 3.12+; it does not install OS drivers.

## GUI cannot open / Tk・画面の問題

Install Python with Tk support, use `sh scripts/setup_za.sh` (Linux) or the Windows setup steps, and run `doctor`. A headless Linux session needs an actual desktop/display for GUI use; the CLI does not require a GUI window. For automated tests, use an owned Xvfb display.

An existing `.venv-za` keeps its original Python even when `PYTHON` selects a newer interpreter. Setup checks that existing environment before changing dependencies (Python 3.10+ for the GUI; Linux/Python 3.12+ for `--live`). If rejected, preserve/rename the old environment and create a new one with a supported Python; do not assume rerunning setup upgrades the interpreter.

既存venvのPythonは`PYTHON`指定だけでは更新されません。古い環境が拒否された場合は保全して別名へ移し、対応Pythonで作り直してください。setupは既存環境を勝手に削除しません。

For small/high-DPI screens, use the existing vertical/horizontal scrollbars or keyboard focus; do not lower OS scaling just to fit this application. Advanced connection settings are optional. When an existing interface is reused, the irrelevant temporary-interface-name field is hidden.

壊れた言語設定は上書きせず、OS言語へ戻します。表示切替は可能ですが、保存するには不正な設定ファイルを修復するか別名へ移してください。

## Edits and saved files / 編集と保存

- **Apply is not Save.** Use Save/Save as and inspect the reported path. `.pa9` is decrypted; `.epa9` / `.ea9` is encrypted. A `*` marks unsaved changes.
- **External change or overwrite refusal:** reload or choose a new output name. Do not bypass identity/no-clobber checks. Keep files another program may have modified.
- **Stats differ after editing:** existing files open in manual mode. Explicit recalculation also heals HP/status; it does not fix legality. Stored → party conversion must calculate stats, not just append zeros.
- **Validation passed:** structure/checksum success does not certify full legality or game loading.

「適用」はメモリ上の反映です。「元ファイルへ保存／名前を付けて保存」を行い、実際の保存先を確認してください。外部変更・上書き拒否は再読込みか新しい名前で解消し、検査を無効化しません。既存文書の能力値は手動モードで開き、明示的な再計算はHP・状態異常も全回復します。構造チェックは合法性保証ではありません。

[PA9 editing details](PA9_CREATION_EN.md) · [PA9編集の詳細](PA9_CREATION.md)

## No Wi-Fi in the list / Wi-Fiが一覧にない

The GUI lists adapters visible to **this Linux host**, not a remote Relay or Windows adapter inventory. On Windows, configure the Linux/WSL side first.

LinuxソースZIPの展開フォルダーで、導入済み`.venv-za`を使って読取り確認します。wheelは`.venv-za/bin/za-ldn-trade`へ読み替えます。resetやdriver変更ではありません。

```sh
lsusb
lsusb -t
iw dev
.venv-za/bin/python zatrade.py doctor --backend direct --radio
```

- **USB device absent:** investigate the physical USB link. Try one adapter directly on a different PC port, bypassing hubs/switches, with a known data-capable cable. A physically inserted plug can still fail enumeration. Do not reset a shared controller with storage or input devices attached.
- **USB visible, no PHY:** check the actual driver binding, matching firmware and kernel support. A loaded module alone does not prove it bound to the adapter. WSL needs its own Linux driver; virtual Ethernet is not a substitute.
- **PHY visible, marked busy/blocked:** do not disconnect your Internet/SSH radio to force a test. Use a dedicated adapter. Check actual network use and rfkill; the app does not silently undo those safeguards.
- **Multiple radios:** choose explicitly. Replugging/replacement invalidates the old choice; refresh and select again.

**USB未列挙 → driver/PHY未作成 → 使用中/無効 → 接続失敗**の順で切り分けます。「USBに挿したから通信できる」「driverがload済みだから機器がある」とは判断しません。読取り自体が拒否された場合は不在ではなく未判定です。

## Optional helper build / BSS補助のビルド

```sh
uname -r
cat /lib/modules/$(uname -r)/build/include/config/kernel.release
sh scripts/setup_bss_helper.sh --help
```

- **Headers missing/mismatched:** install headers for the running kernel using your trusted OS package manager. A newer installed headers tree cannot stand in for the kernel still running.
- **Compiler missing:** use the compiler family used by that kernel (GCC, or Clang/LLD). Building does not require root. `PYTHON=python3` or an absolute interpreter path is accepted by the setup helper.
- **Output already exists:** choose a new directory; the tool never overwrites an existing build. Keep `BUILD.json` and `.ko` together. The default `bss-helper/<kernel>/` folder is visible in the GUI folder chooser.
- **Kernel changed or checksum failed:** rebuild from the included source; do not edit the receipt to bypass a mismatch.

ヘッダー・compilerはOS側の準備です。ビルダーはダウンロード、パッケージ導入、moduleロード、driver置換、DKMS設定をしません。[詳しい手順](BSS_HELPER.md)。

## Helper load / cleanup failure

The helper is for AWUS036ACH / `rtw88_8812au`, not ACHM or legacy88XXau. Select it only on the Linux process owning the radio. A valid build receipt is not a signature or a guarantee that Secure Boot permits loading.

- Module-signing/lockdown failures are not bypassed. These instructions do not sign modules or enroll keys.
- A pre-existing module is not borrowed or unloaded. Confirm who owns it rather than starting another copy.
- Cleanup failure retains ownership/recovery state. Do not force an unload, restart a trade, or remove another process's module to make the error disappear.
- The program cannot keep an in-memory recovery owner after SIGKILL, power loss or a kernel failure. Ask the OS administrator to inspect the retained module and adapter state before recovery.

失敗時の時間切れログにはroom SSID/BSSIDを含むコマンド列を表示しません。ただし通常の通信ログやキャプチャ全体が匿名とは限らないので、公開前に確認してください。

## Uncertain trade / 交換結果が未確定

**Do not repeat the trade. Do not power off while the game is trading or saving.** Inspect the console and the received/recovery files reported by the program. A local timeout does not prove that the game rolled back.

「結果未確定」「FinalAgreement後」の警告が出たら、同じ交換を再試行せず実機と保全ファイルを確認してください。報告にはアプリ版、OS、Python、driver/kernel、補助使用の有無、秘密を除いた再現手順を使い、鍵・個体・セーブ・raw captureは添付しないでください。
