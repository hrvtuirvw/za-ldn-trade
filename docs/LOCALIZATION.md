# Localization / 翻訳

## English

The UI supports `ja` and `en`. Japanese source messages act as stable message IDs; English translations live in `zasim/locales/en.json`. `zasim.i18n.tr()` performs named interpolation only after choosing a translation. Do not concatenate user input into a message ID, translate protocol identifiers, or translate values stored in PA9 files.

- Every translation must be non-empty and preserve the source's named fields, conversion and format specification.
- Catalog validation runs at load time; tests check source coverage, orphaned IDs and English text. Remove translations when their source messages are retired; keep dynamically selected labels declared as source literals.
- The GUI rebuilds only its own presentation when idle. It preserves pending fields, the in-memory document, selected tabs and logs. Live jobs and modal dialogs block a language switch.
- Search labels come from the separately attributed `ui_names.json` data, not the message catalog. Selection writes numeric IDs; UI-language changes never translate a stored nickname or trainer name.
- Low-level technical logs and JSON keys remain stable English. OS-native file-dialog buttons follow the OS locale.
- Startup precedence: `gui --language` → `ZA_TRADE_LANG` → saved preference → OS locale (Japanese or English fallback).
- Library callers default to Japanese for compatibility; launchers choose the user's locale. Preference reads never write a file. Language changes save only the preference, not form fields, codes or keys.

Run the product tests with a display:

```sh
xvfb-run -a python -m pytest -q tests/test_i18n.py tests/test_gui_layout.py tests/test_pa9_creation_gui.py
```

Test both languages at normal and high DPI, including 640×480. Check confirmation prompts, validation errors, unsaved edits, new-entity creation, environment checks and normal exit. A successful Tk constructor is not visual verification.

## 日本語

対応言語は`ja`と`en`。日本語の原文をmessage IDとし、英訳を`zasim/locales/en.json`に保持します。差し込みは名前付きplaceholderで行い、原文と訳文の項目・format指定を一致させます。protocolのID、JSONキー、個体に保存するデータを翻訳してはいけません。

廃止した原文の翻訳は削除し、動的に選ぶラベルもソースに文字列として宣言します。GUIの言語切替は処理待機中のみ。未保存の入力・個体・タブ・ログを維持し、表示だけを作り直します。言語設定の保存先にパス、コード、鍵、個体データは書きません。OS標準ファイルダイアログのボタンはOS言語に従い、技術ログやJSONキーは英語を維持します。

両言語の通常/高DPI・小画面、確認dialog、validation、未保存入力、新規作成、環境チェック、正常終了を実画面で検証してください。
