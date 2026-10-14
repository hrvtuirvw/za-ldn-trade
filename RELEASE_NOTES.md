# za-ldn-trade 0.9.2 — prerelease

## 日本語

『Pokémon LEGENDS Z-A』向けにポケモンを作成・編集し、ローカル交換で実機へ送るためのツールです。[frlg-ldn-trade](https://github.com/tornadus/frlg-ldn-trade)に着想を得て、同プロジェクトの通信実装をベースに開発しています。

### ダウンロード

- **初めて使う場合**：`za-ldn-trade-0.9.2.zip`を展開し、[導入手順](docs/USER_GUIDE.md)に従って起動してください。
- Pythonパッケージとして使う場合：`za_ldn_trade-0.9.2-py3-none-any.whl`。
- `za_ldn_trade-0.9.2.tar.gz`はPythonパッケージのソース配布です。
- `SHA256SUMS`でダウンロード内容を確認できます。

**Python 3.10以上、GUIにはTkが必要です。Python同梱の単体exeではありません。**

### この版の変更

- Windowsで正常なファイルの読取りを拒否していたメタデータ比較を修正。ファイル差替え・変更検出の安全策は維持しています。
- 新規PA9ダイアログの詳細表示・サイズ変更後のスクロール範囲と、小さい画面・高DPIでの操作性を修正しました。
- Windowsで大量ログの末尾表示が停止する問題と、Gitの改行変換で名前カタログの整合性検査が失敗する問題を修正しました。
- 配布物の元のZIPメンバー名を正規化前に検査するよう強化。通信・保存・停止・既存設定・起動コマンドは変更していません。

### 動作確認と制限

- リリースCIはWindows／Ubuntu × Python 3.10・3.12・3.14の全6構成で、回帰テスト・配布物検査・ソース外へ導入したCLI/GUIを確認します。このタグの全検査に合格した配布物だけを公開します。[修正内容の検証済み基準](https://github.com/hrvtuirvw/za-ldn-trade/actions/runs/38583042096)・[リリースCI](https://github.com/hrvtuirvw/za-ldn-trade/actions/workflows/release.yml)・[検証情報](docs/TESTING.md)。
- WSL・他の機材・この版での実機交換／保存／復旧は未検証です。過去の開発構成での実機結果とは区別しています。
- 無線にはLinux、Python 3.12以上、専用の対応Wi-Fiと権限が必要です。任意のBSS補助には、一致するカーネルヘッダーとビルドツールも必要です。
- PA9の構造チェックは完全な合法性判定ではなく、ゲーム内の読込みや交換を保証しません。交換結果が不明な場合はすぐに再試行せず、実機と受け取った個体の保存ファイルを確認してください。交換・保存中は電源を切らないでください。
- ROM・鍵・個人セーブ・個体データ・ビルド済みカーネルモジュールは同梱していません。

[使い方](docs/USER_GUIDE.md) · [無線対応表](docs/WIRELESS_COMPATIBILITY.md) · [変更履歴](https://github.com/hrvtuirvw/za-ldn-trade/blob/v0.9.2/CHANGELOG.md)

## English

Create and edit Pokémon for Pokémon LEGENDS Z-A, then send them to a console through local trading. Inspired by [frlg-ldn-trade](https://github.com/tornadus/frlg-ldn-trade) and built on its communication implementation.

### Downloads

- **Start with `za-ldn-trade-0.9.2.zip`**, extract it and follow the [setup guide](docs/USER_GUIDE_EN.md).
- Python package: `za_ldn_trade-0.9.2-py3-none-any.whl`.
- Python source distribution: `za_ldn_trade-0.9.2.tar.gz`.
- Verify downloaded files with `SHA256SUMS`.

**Python 3.10+ and, for the GUI, Tk are required. These are not standalone executables with Python bundled in.**

### Changes

- Fix Windows metadata comparison rejecting valid file reads while preserving replacement/change detection and other file-safety guards.
- Correct new-PA9 dialog scroll extents after disclosure and resizing, including small/high-DPI layouts.
- Fix stalled tail-following for large Windows GUI logs and catalog integrity failures caused by Git newline conversion.
- Validate original ZIP member names before host normalization. Keep communication, saving, cancellation, existing settings and entrypoints unchanged.

### Verification and limits

- Release CI checks all six Windows/Ubuntu × Python 3.10/3.12/3.14 combinations: product regressions, distribution integrity and CLI/GUI installed outside source. Only assets that pass this tag's release gates are published. See the [verified fix baseline](https://github.com/hrvtuirvw/za-ldn-trade/actions/runs/38583042096), [release workflow](https://github.com/hrvtuirvw/za-ldn-trade/actions/workflows/release.yml) and [testing notes](docs/TESTING.md).
- WSL, other hardware and physical exchange/save/recovery with this release remain unverified. Earlier development hardware results are separate.
- Wireless requires Linux, Python 3.12+, a dedicated compatible adapter and privileges. The optional BSS helper also requires matching kernel headers and build tools.
- PA9 validation is not a complete legality check or a guarantee of game loading/trading. Do not immediately retry an uncertain trade: inspect the console and saved received file first. Never power off during trading or saving.
- No ROMs, keys, personal saves, Pokémon records or compiled kernel module are bundled.

[User guide](docs/USER_GUIDE_EN.md) · [Wireless compatibility](docs/WIRELESS_COMPATIBILITY_EN.md) · [Changelog](https://github.com/hrvtuirvw/za-ldn-trade/blob/v0.9.2/CHANGELOG.md)
