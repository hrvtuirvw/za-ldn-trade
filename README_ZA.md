# za-ldn-trade — CLI・接続リファレンス

[日本語ホーム](README_JA.md) · [English](README.md) · [ユーザーガイド](docs/USER_GUIDE.md)

対象は **Z-A 2.0.2**。PA9作成・編集は無線不要、実通信にはLinux/Python 3.12以上と専用の対応Wi-Fiが必要です。この文書の`phy0`・`wlan0`・パス・8桁コードは例であり、あなたの環境の値に置き換えます。

## コマンド例の前提

ソースZIPを展開し、[導入手順](docs/USER_GUIDE.md#導入)を完了してから、そのフォルダーで実行します。例はLinuxの`.venv-za`を明示しています。Windowsソース版は`.\.venv-za\Scripts\python.exe zatrade.py`、wheel版は仮想環境内の`zatrade`へ読み替えてください。システムPythonや別venvへ切り替えないでください。

## 最初に確認する

```sh
.venv-za/bin/python zatrade.py --version
.venv-za/bin/python zatrade.py doctor
.venv-za/bin/python zatrade.py doctor --backend direct --radio
.venv-za/bin/python zatrade.py --help
```

`doctor`は読取り専用で、鍵の読取り・スキャン・接続・無線設定変更はしません。必要条件が揃うことと、実機で交換が成功することは別です。

機材の条件は[無線対応表](docs/WIRELESS_COMPATIBILITY.md)、版ごとの実証範囲は[検証情報](docs/TESTING.md)を参照してください。この配布版の実機交換・保存は未検証です。

## PA9を新規作成・編集・検査

```sh
.venv-za/bin/python zatrade.py create-pa9 --species 152 --language 2 \
  --ot-name Test --tid 1 --sid 2 --output starter.pa9
.venv-za/bin/python zatrade.py inspect-pa9 starter.pa9
.venv-za/bin/python zatrade.py edit-pa9 starter.pa9 --output edited.epa9 \
  --set nickname=ZA-Test --set nature=3 --set iv_hp=31
```

新規ファイルの暗黙上書きはしません。承認して置き換える場合だけ`--force`を指定します。`edit-pa9 --in-place`は元ファイルの置換を明示する別指定です。保存時には再検査し、対象が変わっていれば拒否します。

`.pa9`は復号済み、`.epa9`/`.ea9`は暗号化出力。交換には完全な344バイトparty PA9が必要です。328バイトstoredデータをゼロ埋めで代用しません。明示的なparty変換は実際の能力値計算を伴います。[作成・計算・合法性の区別](docs/PA9_CREATION.md)。

## 通常ユーザーGUI＋Linux Relay

GUI全体をrootで動かさず、無線処理だけを管理者で起動する構成です。Python環境は`sh scripts/setup_za.sh --live`で準備します。

```sh
# Linux terminal 1: 専用の無線機を所有するバックエンド
sudo .venv-za/bin/python -s -B zatrade.py relay-serve \
  --keys /path/to/your/prod.keys --phy phy0 --interface wlan0 --single-interface

# Linux terminal 2: 通常ユーザーGUI
sh scripts/start_za_gui.sh --backend relay
```

既存物理インターフェイスを使わない対応構成では`--single-interface`を省略し、一時interface名を指定します。**インターネットやSSHを担うPHYを選ばないでください。** 実通信は一時的にNetworkManager管理、interface状態、選択ネットワーク設定を変更し、終了時に復旧確認します。強制終了では復旧を保証できません。

RelayはIPv4 loopbackのみ。LAN/Internetへ直接公開せず、別Linux機ならSSHで転送します。

```sh
ssh -N -L 24872:127.0.0.1:24872 user@linux-host
```

Relayは同じホストのlocal processを信頼するため、非信頼ユーザーと共有する環境では起動しません。鍵は無線を所有するLinux側に置き、Relay clientへ転送しません。

## AWUS036ACHの任意BSS補助

```sh
sh scripts/setup_bss_helper.sh
# 出力されたフォルダーを確認（例）
.venv-za/bin/python zatrade.py bss-helper check --bundle ./bss-helper/KERNEL_VERSION
```

通常ドライバ`rtw88_8812au`に追加するもので、ドライバ交換ではありません。使用する場合だけ、上のLinux側`relay-serve`等へ`--bss-helper /absolute/bundle/directory`を追加します。GUI直接接続なら詳細設定の「参照」で選択できます。詳細は[BSS補助の導入と後片付け](docs/BSS_HELPER.md)。

## WSL2を同じPCで使う

WSL2の仮想Ethernetではraw Wi-Fi操作はできません。専用USB Wi-FiをWSLへ渡し、**WSLカーネルに対応driver/firmwareが必要**です。USBが表示されることと、無線PHYが使えることも別です。ACHとACHMを混同せず、legacy88XXauのcontrol-port不足を属性削除で回避しないでください。

[MicrosoftのUSB接続手順](https://learn.microsoft.com/windows/wsl/connect-usb)に従い、管理者PowerShellで実際のBUSIDを指定します。

```powershell
usbipd list
usbipd bind --busid BUSID
usbipd attach --wsl --busid BUSID
```

Linux/WSL内でPython 3.12以上、`iw`、iproute2、NetworkManager、procpsとlive依存を準備し、上の読取り診断を通してからRelayを起動してください。任意のWSL標準カーネルが必要な機能を持つとは保証しません。

補助なしの通常Relay用PowerShell launcherも同梱しています。実行ポリシーは管理者の規則に従い、勝手に緩めないでください。

```powershell
.\scripts\start_za_relay_wsl.ps1 -Setup -Keys ~/.switch/prod.keys
.\scripts\start_za_gui.ps1
```

既存interfaceを明示するなら`-Phy phy0 -Interface wlan0 -SingleInterface`、1sessionだけなら`-Once`。BSS補助を使う場合はLinux/WSL内でビルドし、Linux側のCLIで`--bss-helper`を指定します。Windows側へそのパスを指定しても転送されません。

## 接続だけを確認する

実機をローカル交換の待受けにし、同じ8桁コードで`handshake`を実行します。**個体提示・確定・保存コマンドは送りません**が、ネットワークには参加します。接続・復旧確認と不可逆な交換を分けるための診断です。

```sh
sudo .venv-za/bin/python -s -B zatrade.py handshake \
  --keys /path/to/your/prod.keys --phy phy0 --interface wlan0 --single-interface \
  --link-code 12345678 --output handshake-capture.jsonl
```

詳細な引数は`handshake --help`。出力は公開前に自分で確認し、通信コードや識別情報をissueに貼らないでください。

## 交換する

実機Z-A 2.0.2で待受けを作り、PC側にparty PA9と新しい受領ファイル名を指定します。

```sh
.venv-za/bin/python zatrade.py trade --relay 127.0.0.1:24872 \
  --offer my-party.epa9 --output received.epa9 --link-code 12345678
```

相手個体の確認後、`yes`と入力した場合だけ確定します。実機側でも渡してよい個体を選択・確定してください。`--yes`は明示的な自動承認なので、通常操作では付けません。

完了には双方Finalだけでなく、参加者同期・保存同期・PC受領ファイルの耐久保存・後片付けが必要です。**FinalAgreement後の失敗は結果未確定になり得ます。再交換せず、実機と保存されたファイルを確認してください。** CLIは未完・未確定なら終了3、入力/実行エラーなら終了2を返します。

`--title-version`の既定は2.0.2（公開JoinタグLE16=6）。旧版用タグが存在することは旧版での交換保証ではありません。

## 採取とオフライン解析

```sh
# 自分の待受けに参加して採取。個体交換は実行しない。
.venv-za/bin/python zatrade.py probe --relay 127.0.0.1:24872 \
  --link-code 12345678 --output captures/session.jsonl
.venv-za/bin/python zatrade.py analyze captures/session.jsonl

# オフラインcodec
.venv-za/bin/python zatrade.py trade-encode confirm --sequence 0 \
  --output confirm.archive --wire-output confirm.wire
.venv-za/bin/python zatrade.py trade-analyze --input-kind wire-payload --file confirm.wire
```

キャプチャは識別情報を含み得ます。既存出力の置換には明示承認が必要です。生ダンプscanは全入力をRAMへ読むため、キャプチャの逐次解析と混同しないでください。

## 実装・公開参照

Pia 6.40/header v16、AES-GCM、Zstandard、Reliableの再送/順序/ACK、10種protocol registry、BroadcastReliable、UserData、BoxTrade、SyncSaveを実装しています。暗号化やchecksumが通ることだけではゲーム受理・合法性・保存完了を証明しません。

[LDN](https://github.com/kinnay/LDN) · [NintendoClients/Pia](https://github.com/kinnay/NintendoClients/wiki/Pia-Protocol) · [PKHeX](https://github.com/kwsch/PKHeX) · [ライセンス・出自](THIRD_PARTY_NOTICES.md)
