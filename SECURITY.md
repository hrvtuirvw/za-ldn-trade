# Security and private data

Do not open a public issue containing console keys, saves, real PA9 records, unredacted captures, personal identifiers or core dumps. Use the repository's private vulnerability reporting feature where enabled; otherwise contact its maintainer without attaching secrets.

The GUI runs as an ordinary user. Direct Linux radio operation and a Linux relay require privileges and a dedicated interface. The relay binds to loopback only; do not expose it to a LAN or Internet. Local loopback clients are trusted, so do not run it on a shared untrusted machine. Use an SSH local-forward for a separate Linux host.

GUI startup and `doctor` do not read key contents or mutate a radio. A successful installation check is not a live-connection guarantee. Do not run the GUI as root merely to edit a PA9 file.

After the irreversible FinalAgreement boundary, cancellation or failure may leave an indeterminate console outcome. Preserve received/recovery artifacts, inspect both endpoints and do not automatically retry. Normal cleanup cannot protect against power loss, a kernel failure or forcibly killed processes.

## 日本語

公開issueへ鍵・ROM・セーブ・実個体・未加工の通信ログ・個人情報を添付しないでください。脆弱性はprivate reportingが有効ならそちらへ、無効なら秘密を添付せずmaintainerに相談してください。

GUI/PA9編集は通常ユーザーで実行します。無線backendだけを専用PHY・管理者権限で動かし、relayはloopbackのまま使用してください。結果未確定の交換は再試行せず、実機と保全ファイルを確認します。

## Optional kernel helper

The BSS helper is off by default and built from the included GPL2 C source for the running kernel. A local SHA-256 receipt is not a trusted-party signature. Do not load a binary/receipt supplied by an untrusted person. The application never disables module-signing policy, forces an unload, or adopts a pre-existing foreign module. A cleanup failure or killed process requires ownership/state inspection, not an automatic repeat trade. Build it as an ordinary user; grant privileges only to the radio-owning process.

Optional-helper timeout errors suppress peer-bearing command arguments and exception chains. This does not make full communication logs/captures anonymous. Review any diagnostic before public disclosure.

任意補助のビルド記録は署名証明ではありません。同梱ソースから自分でビルドし、他人のbinary/記録を信用してrootでロードしないでください。署名制限の無効化・強制unload・他processのmodule取り外しはしません。
