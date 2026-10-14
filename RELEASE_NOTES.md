# za-ldn-trade 0.9.1 — prerelease

## 日本語

『Pokémon LEGENDS Z-A』向けにポケモンを作成・編集し、ローカル交換で実機へ送るためのツールです。[frlg-ldn-trade](https://github.com/tornadus/frlg-ldn-trade)に着想を得て、同プロジェクトの通信実装をベースに開発しています。

### ダウンロード

- **初めて使う場合**：`za-ldn-trade-0.9.1.zip`を展開し、[導入手順](docs/USER_GUIDE.md)に従って起動してください。
- Pythonパッケージとして使う場合：`za_ldn_trade-0.9.1-py3-none-any.whl`。
- `za_ldn_trade-0.9.1.tar.gz`はPythonパッケージのソース配布です。
- `SHA256SUMS`でダウンロード内容を確認できます。

**Python 3.10以上、GUIにはTkが必要です。Python同梱の単体exeではありません。**

### この版の変更

- ツール名を`za-ldn-trade`に統一。GUI、診断表示、配布名、日英文書を更新しました。
- wheelの起動コマンドは`za-ldn-trade`／`za-ldn-trade-gui`、Windowsのソース版ランチャーは`Start-za-ldn-trade.cmd`です。ソースからの`python zatrade.py`は引き続き使えます。
- 既存の言語設定を引き継ぐため、設定フォルダーの名前は変更していません。通信の識別値・保存・停止・復旧処理も変更していません。
- READMEを「作って実機へ送る」流れに整理。上流の出典とライセンスを保持しました。

### 動作確認と制限

- ローカル検証環境はLinux x86-64／Python 3.14.8／隔離Tk画面です。Windows/WSL・他runtime/機材・この版での実機交換／保存／復旧は未検証です。過去の開発構成での実機結果とは区別しています。[検証情報](docs/TESTING.md)。GitHub Actionsの結果はリポジトリのActions欄で確認してください。
- 無線にはLinux、Python 3.12以上、専用の対応Wi-Fiと権限が必要です。任意のBSS補助には、一致するカーネルヘッダーとビルドツールも必要です。
- PA9の構造チェックは完全な合法性判定ではなく、ゲーム内の読込みや交換を保証しません。交換結果が不明な場合はすぐに再試行せず、実機と受け取った個体の保存ファイルを確認してください。交換・保存中は電源を切らないでください。
- ROM・鍵・個人セーブ・個体データ・ビルド済みカーネルモジュールは同梱していません。

[使い方](docs/USER_GUIDE.md) · [無線対応表](docs/WIRELESS_COMPATIBILITY.md) · [変更履歴](CHANGELOG.md)

## English

Create and edit Pokémon for Pokémon LEGENDS Z-A, then send them to a console through local trading. Inspired by [frlg-ldn-trade](https://github.com/tornadus/frlg-ldn-trade) and built on its communication implementation.

### Downloads

- **Start with `za-ldn-trade-0.9.1.zip`**, extract it and follow the [setup guide](docs/USER_GUIDE_EN.md).
- Python package: `za_ldn_trade-0.9.1-py3-none-any.whl`.
- Python source distribution: `za_ldn_trade-0.9.1.tar.gz`.
- Verify downloaded files with `SHA256SUMS`.

**Python 3.10+ and, for the GUI, Tk are required. These are not standalone executables with Python bundled in.**

### Changes

- Rename the tool to `za-ldn-trade` across the GUI, diagnostic output, distribution names and bilingual documentation.
- Wheel commands are now `za-ldn-trade` and `za-ldn-trade-gui`. The Windows source launcher is `Start-za-ldn-trade.cmd`. Source-based `python zatrade.py` remains available.
- Keep the existing language-preference folder, protocol identities, saving, cancellation and recovery behavior unchanged.
- Reorganize the README around creating a Pokémon and sending it to a console. Preserve upstream attribution and licenses.

### Verification and limits

- Local verification uses Linux x86-64 / Python 3.14.8 / an isolated Tk display. Windows/WSL, other runtimes/adapters and physical exchange/save/recovery with this release remain unverified. Earlier development hardware results are separate; see [testing notes](docs/TESTING.md). Check the repository's Actions tab for GitHub CI results.
- Wireless requires Linux, Python 3.12+, a dedicated compatible adapter and privileges. The optional BSS helper also requires matching kernel headers and build tools.
- PA9 validation is not a complete legality check or a guarantee of game loading/trading. Do not immediately retry an uncertain trade: inspect the console and saved received file first. Never power off during trading or saving.
- No ROMs, keys, personal saves, Pokémon records or compiled kernel module are bundled.

[User guide](docs/USER_GUIDE_EN.md) · [Wireless compatibility](docs/WIRELESS_COMPATIBILITY_EN.md) · [Changelog](CHANGELOG.md)
