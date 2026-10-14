# 新規PA9作成

[English](PA9_CREATION_EN.md) | **日本語**

元セーブ・既存PA9・ROM・鍵を使わず、独立した **344B party PA9** を作成します。Python/Tk版GUIとCLIは同じ生成コアを使います。[導入手順](USER_GUIDE.md)。

## 使い方

種族は名前（日英）・全国番号で検索できます。検索は候補一覧の絞込みだけで、選択済みの種族を自動変更しません。候補から作成したい種族を選んでください。「詳細（任意）」に親の性別・出会い日・ニックネーム・技をまとめ、折りたたんでも入力を維持します。カスタムの必須出会い条件は常時表示します。

GUIの「PA9編集 → 新規作成」で種族、親名、TID16/SID16、言語、出会いを指定し、「作成して編集 → 保存」。新規IDは「新しいIDを生成」で明示的に入力できます。新規作成だけではファイル保存も交換も行いません。キャンセルでは既存文書を置き換えません。

以下はLinuxソースZIPの展開フォルダーで実行します。[Windows／wheelのコマンド対応](USER_GUIDE.md#cliで使うpython)。

```sh
.venv-za/bin/python zatrade.py list-species
.venv-za/bin/python zatrade.py create-pa9 --species 152 --ot-name トレーナー --tid 12345 --sid 54321 --output new-chikorita.pa9
.venv-za/bin/python zatrade.py create-pa9 --species 4 --language 2 --ot-name Trainer --tid 12345 --sid 54321 --level 50 --output new-charmander.epa9
```

- 新規作成の`--species`は**全国図鑑番号**。既存編集の`species_internal`とは区別します（Gen9後半は対応が異なります）。フォームは`--form`。
- TID/SIDはそれぞれ0–65535の内部16bit値。ゲーム表示の6桁IDをそのままTIDへ入れる指定ではありません。
- `--encounter starter`（既定）: フシギダネ/ヒトカゲ/ゼニガメ/チコリータ/ワニノコ/ポカブ。出会いLv5・指定場所・モンスターボール、3個以上の31IV、非色違い、公開モデルに従ったEC/PID/特性/性別/性格/サイズを生成。
- 現在Lvは出会いLv以上、100まで。経験値は種族の成長曲線で算出。既定技は出会い技または現在Lvで覚える技。`--moves 33,39`でその種族のレベル技/TM範囲から指定できます。PP、Plusフラグも初期化。
- `--encounter custom`: 同梱personalに存在し戦闘専用でない490種族・フォームの**下書き**。これは490種の別ポケモンという意味ではありません。`--level --met-level --met-location`を明示し、正しい入手経路かは別途調べてください。野生出会い検索、進化経路解決、固定NPCギフト、配信・色違い生成は実装していません。

```sh
# 出会いIDの妥当性はこのコマンドでは検証されない下書き
.venv-za/bin/python zatrade.py create-pa9 --encounter custom --species 25 --level 20 --met-level 10 --met-location 1 --ot-name トレーナー --tid 12345 --sid 54321 --output draft.pa9
```

`--met-date YYYY-MM-DD`、`--nickname`、`--ball`、`--ot-gender`、`--language`も指定できます。日本語・韓国語・中国語の親名/任意ニックネームは6 UTF-16単位、他言語12単位まで。既定の日付はローカルの今日、seedは毎回新規乱数。`--seed`は再現試験向けです。名前の禁止語判定や全言語の入力可能文字の判定は含みません。

保存先既存ファイルは既定で拒否。CLIは明示の`--force`だけで置換を許可します。既存の同一パスlock・外部変更検知・原子的保存を共用します。`.pa9`は復号、`.epa9`/`.ea9`は暗号化。

## 能力値再計算

新規文書は「能力値を自動計算」がON。適用/保存時、種族/フォーム・経験値・IV/EV・補正性格（ミント）・Hyper TrainingからHP/5能力値/レベルを計算します。Lv欄の変更は成長曲線から経験値へ変換。Lvと経験値を同時に矛盾させた入力は拒否します。自動計算中の能力値キャッシュは読取り専用です。

既存文書は互換性のため手動編集モードで開きます。GUIの「能力」タブで「再計算」または自動計算ON、CLIの`--recalculate`で明示的に計算できます。**再計算は現在HPと状態異常も全回復**します。技・特性・出会い・RNG相関・履歴の修復ではありません。種族変更後はそれらも確認してください。

```sh
.venv-za/bin/python zatrade.py edit-pa9 input.pa9 --output calculated.pa9 --set iv_hp=31 --set ev_hp=252 --recalculate
.venv-za/bin/python zatrade.py edit-pa9 input.pa9 --output level50.pa9 --level 50
.venv-za/bin/python zatrade.py edit-pa9 stored.pa9 --output party.pa9 --party
```

328B stored → 344B partyは明示的な`--party`/GUI確認が必要で、必ず計算します。未知の種族/フォーム、戦闘専用フォーム、EV過多、範囲外経験値等は拒否。ゼロ追加だけで成功にはしません。通常の編集保存は入力サイズを維持します。

## 検証の意味

1. **構造**: サイズ・sanity・checksum・暗号化往復。
2. **公開モデル整合**: personal/成長曲線/能力値・技・出会いプリセット生成規則。
3. **合法性**: アプリには完全な判定エンジンを同梱しません。結果は常に`not_checked`。外部PKHeXで特定生成サンプルを合格確認しても、自由編集した全個体の合法性は保証しません。
4. **ゲーム生成可能性・読込み・保存**: 新規生成個体のゲーム読込み・保存は未検証です。別個体による交換成功は生成機能の証明ではありません。
5. **実機交換**: 新規生成個体では未検証。作成・保存だけでは一切無線を開始しません。

## データ出自・再生成（保守者のみ）

[第三者ライセンス](../THIRD_PARTY_NOTICES.md)を参照。データは公開PKHeX Core 26.08.26の固定コミット`e15d2467b32da7bc26ce7cc8e5c4ede32740e20a`から取得したものです。私有のROM解析結果やセーブを入力にしていません。

公開アーカイブ: https://codeload.github.com/kwsch/PKHeX/tar.gz/e15d2467b32da7bc26ce7cc8e5c4ede32740e20a

アーカイブと重要入力のSHA-256は[provenance](../zasim/data/za_creation_provenance.json)で照合し、空ディレクトリへ展開します。標準の.NET 10 SDKで、配布ソース内の次のツールを実行できます（これは製品利用者には不要）。`PKHeXSource`は展開した公開ソースのルートを指します。

```console
dotnet run --project scripts/pa9_reference -c Release -p:PKHeXSource=/path/to/public/PKHeX -- export /new/path/za_creation.json
dotnet run --project scripts/pa9_reference -c Release -p:PKHeXSource=/path/to/public/PKHeX -- vectors /new/path/pa9_creation_reference.json
dotnet run --project scripts/pa9_reference -c Release -p:PKHeXSource=/path/to/public/PKHeX -- verify /path/to/generated-test-pa9s
```

export/vectorsは既存出力を上書きしません。生成JSONのハッシュを照合してください。APIやデータを更新する場合はpin・provenance・回帰fixtureをレビューして一緒に更新します。検証用C#のbin/objは公開payloadへ含めません。
