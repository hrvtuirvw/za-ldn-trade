# 無線アダプター・構成別の検証状況

[English](WIRELESS_COMPATIBILITY_EN.md) | **日本語** · [導入](USER_GUIDE.md)

最終実機観測: 2026-10-10（新しい版の検証を意味しません）

## 基準機材はAWUS036ACH。ただし構成まで区別する

**基準機材はAWUS036ACH / RTL8812AU / `rtw88_8812au`。この配布版の実機交換・保存は未検証です。** 開発構成での交換成功と0.8.6での補助なし接続成功は、版・構成を限定した過去の証拠です。[条件と実証範囲](TESTING.md)を参照してください。

任意の[BSS補助](BSS_HELPER.md)を同梱しますが、ドライバの代替や動作保証ではありません。その他の機材も未検証です。monitor対応だけでは判定できず、PA9作成・編集には無線も鍵も不要です。

| 機材 / チップ | Linuxドライバ | 本プロジェクトでの根拠 | この配布版 |
|---|---|---|---|
| ALFA **AWUS036ACH** / RTL8812AU | `rtw88_8812au` | 記録した開発構成で実交換・保存成功。0.8.6補助なしではfull handshake・正常終了・復旧を確認 | 基準機材。この配布版の実機交換・保存は未検証 |
| 同じAWUS036ACH | legacy `88XXau` / `8812au` / `rtl8812au` / `rtl88xxau` | 必須custom control-port不足を確認 | このlegacy構成はLDN認証不可 |
| ALFA **AWUS036ACHM** / MT7610U | `mt76x0u` | 本プロジェクトでは未検証。上流FRLGには高信頼・デモの報告 | 未検証・保証なし。ACHとは別製品 |
| Realtek RTL8821CE / PCIe | `rtw88_8821ce` | 上流FRLGの高信頼報告のみ | 未検証・保証なし |
| AMD RZ616 / M.2 | `mt7921e` | 上流FRLGで遅延・停止の報告 | 未検証・保証なし |
| Intel AX200 / M.2 | `iwlwifi` | 上流FRLGでIP割当て問題の報告 | 未検証・保証なし |
| Atheros AR9271 / USB | `ath9k_htc` | 上流FRLGでIP割当て問題の報告 | 未検証・保証なし |
| その他 | 各種 | ブランド・規格・monitor対応だけでは判定できない | 未検証・保証なし |

機種名が同じでもhardware revisionやdriver/firmware/kernelが変われば別構成です。過去に使った補助が常に必須とは限らず、USB IDだけでも同一動作を保証しません。

上流の固定参照: [FRLG README（13809c21）](https://github.com/tornadus/frlg-ldn-trade/blob/13809c21b6e992097f98453b7cbc9e2bc30bbf7c/README.md#tested-wifi-cards)。**別ゲームの利用者報告**であり、本製品のZ-A実証や購入推奨ではありません。

## 必要なのは速度ではなく低レベル機能

現行ソフトはSwitchのLDNへ**参加するstation/client**です。家庭のルーターに接続する普通のWi-Fi通信、Bluetooth、Switch Online経由ではありません。Wi-Fi 5/6/6E/7、最大Mbps、アンテナ本数、一般的な「packet injection対応」は互換性の証明になりません。

必要条件は次のとおりです。

- Linuxと対応driver/firmware、Python 3.12以上、live依存。実通信時は無線プロセスだけrootで起動。GUIは通常ユーザーでよい。
- **managedとmonitor**の両interface mode。
- monitorでの802.11 action frameの正しい受信・送信。機能フラグだけでは実効性を検査できない。
- managedでの認証・association、**CCMP**、LDNのcustom control-port **EtherType 0x88B7**。
- **CONTROL_PORT_OVER_NL80211**。custom EtherTypeとは別の能力なので、両方必要。片方からもう片方を推定しない。
- 現行の標準探索は **2.4GHzの1/6/11ch**。それらの周波数を地域の規制内で利用できること。依存LDNライブラリに5GHz定義があっても、標準GUI/CLIが5GHz探索対応とは限らない。5GHz/6GHzしか使えない構成は対象外。
- 選択したPHYを交換専用に使えること。インターネット・SSH・既存接続を担う同じPHYを選ばない。VIFを分けても物理無線のチャンネルを独立に扱えるとは限らない。
- 通常は一時的なmonitor/station VIFを順に作る。対応driverで既存netdevを再利用する場合は`--single-interface`。これは**物理無線1台の中のinterface方式**であり、2本のUSBアダプターが必須という意味ではない。

本製品はLDNホスト/AP実装を使わないので、上流LDNのホスト用active/passive monitor 2-interface構成やAP modeを、参加クライアントの必須条件に混同しないでください。

Linuxなら内蔵PCIe/M.2も原理上対象です。ネット接続を維持したい場合は、取り外せる専用USB機＋有線LAN等が扱いやすい構成です。国設定の偽装、規制解除、必要なCONNECT属性の削除を回避策にしません。

## Windows / WSL2

WindowsのGUI/PA9編集は無線不要です。実通信はLinuxかWSL2のRelayが担当します。

- WSL2の`eth0`や通常の仮想Ethernetは、Windows Wi-Fiのraw/nl80211能力を提供しません。
- 同じPCなら対応USB Wi-Fiを`usbipd-win`でWSL2へ渡し、**WSLカーネル内に実際のLinux driver/firmwareが必要**です。USBに見えることと無線PHYが作られることは別です。
- 通常のWSL2には内蔵PCIe/M.2 Wi-FiをUSB転送できません。内蔵機を使うならnative Linux機＋SSH転送Relay等を使います。
- USB転送中はWindows側でそのアダプターを使えません。WSL更新だけで任意のdriverが追加されるとは保証しません。
- 診断は**Relayを動かすLinux/WSL内**で実行します。Windows側GUIの環境チェックは、遠隔Relayの無線機器を検査しません。

USB導入手順は[詳細README](../README_ZA.md#wsl2を同じpcで使う)、OS側の説明は[Microsoft WSL USBドキュメント](https://learn.microsoft.com/windows/wsl/connect-usb)を参照してください。WSL実機でのこの版の検証は未実施です。

## 非破壊の購入後・接続前診断

LinuxソースZIPの[導入](USER_GUIDE.md#導入)後、展開フォルダーで実行します（wheelなら`.venv-za/bin/za-ldn-trade`へ置き換える）:

```sh
.venv-za/bin/python zatrade.py doctor
.venv-za/bin/python zatrade.py doctor --backend direct --radio
# 複数あるときは上の一覧で現在の専用PHY名を確認して明示
.venv-za/bin/python zatrade.py doctor --backend direct --radio --phy phy1
.venv-za/bin/python zatrade.py doctor --backend direct --radio --phy phy1 --json
```

GUIは「環境チェック」→PHYを選択→「無線詳細（このPC）」。長い結果はスクロールでき、診断中もウィンドウを閉じられます。既存GUIを自動更新・再起動する機能ではありません。

- 普通の`doctor`は従来どおり依存・sysfs一覧だけ。`--radio`で初めてローカルの読取り照会を行う。
- `iw phy NAME info`、route/addressの表示、sysfs、**GETFAMILY/GET_WIPHYだけ**のローカルnetlink照会を使用。`iw`はcustom EtherTypeフラグを表示しないため、別途カーネルへ確認する。
- **スキャン・電波送受信・join・鍵の読取り・driver導入・rfkill解除・接続切断・無線設定変更はしない。sudoも不要。** 詳細診断のnetlink socketはローカルカーネル照会で、無線frame送信用socketではない。
- 複数PHYでは性能から勝手に1台を選ばず停止する。他PHYの良い能力を選択PHYへ流用しない。取得失敗・情報の矛盾・抜差し中は未判定。
- 同じPHYの別interfaceも含めdefault route、IP address、carrier、rfkillを確認。使用中/無効なら自動修正しない。この読取り時点のチェックは実行時の所有権/安全な復旧チェックの代わりではない。
- MAC、SSID、IPの生値やUSB serial、鍵を診断結果へ出力しない。公開報告前にはホスト環境の情報が含まれるJSONを自分でも確認する。

`--radio`の終了コード:

| コード | 意味 |
|---|---|
| 0 | 必要な能力表示・live依存・読取り時点の専用状態を確認。**電波/接続/交換の成功ではない** |
| 2 | 機能不足、依存不足、使用中、rfkill、機器未検出、またはLinux側でない |
| 3 | 未判定、読み取れない、または複数候補で選択が必要 |

JSONの`wireless.status = requirements_present_not_verified`も「動作検証済み」ではありません。`action_frame_io_tested`、`standard_exchange_verified`、`live_connection_tested`はこの診断では常にfalseです。古いカーネル/権限制約などで読取れない場合は安全側に未判定を返します。

WSL起動スクリプトもこの共通診断を利用し、必要条件を確認できない場合はRelayの無線操作へ進みません。成功しても実機互換性の保証は表示しません。

## 検証結果を報告する

機種・driver・版・接続方式を明示し、[実機検証の段階](TESTING.md#before-calling-a-configuration-physically-verified)を配布物そのもので確認してください。接続だけの試験と不可逆な交換を分け、未達の構成はexperimental/未検証と表示します。

参照: [LDN requirements/design（6ee37bf0）](https://github.com/kinnay/LDN/blob/6ee37bf0fa9f7ab1baa0c67e696ed28b9e2cb120/README.md)、[Linux nl80211 UAPI](https://docs.kernel.org/networking/netlink_spec/nl80211.html)。
