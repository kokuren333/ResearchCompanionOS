# Research Companion OS — 日本語ガイド

[English](README.en.md) · [プロジェクト概要](README.md)

Research Companion OSは、長期研究をローカルで継続するための研究伴走アプリです。普段はChatGPTのようなチャットUIだけを使い、重要な知見をSQLiteへ構造化して保存します。保存した知識は検索やAgent Context Packetに使われ、Obsidianにはプロジェクト・Memory・会話を相互に辿れる形で出力されます。研究PDFは分野別にVaultへ保存し、原PDFを壊さずにレイアウトを維持した日本語PDFを生成できます。

## 目的と考え方

長期研究では、毎回チャット履歴を最初から読み直すと、決定・失敗・未解決の問いが失われます。このアプリは「今の会話」と「長期的に残す記録」を分離します。

1. チャットで調査・開発を進める。
2. Decision、Failure、Question、Evidence、FindingなどをMemoryとして保存する。
3. 検索とContext Packetで、次のエージェント実行に必要な知識だけを選ぶ。
4. Obsidianへ、プロジェクト・Memory・元会話をリンクした読みやすい投影を作る。

SQLiteが機械側の正本です。Obsidianは人間が読むための投影であり、タグ付きのユーザーノートを取り込むための入口でもあります。

## Windowsでの起動

### リリースZIPを使う

ZIPを展開して `Research Companion.exe` を起動してください。PythonバックエンドはTauriアプリに同梱されているため、配布版の実行にPythonは不要です。バックエンドは動的に割り当てたlocalhostポートだけで待ち受け、Windows版は黒いコマンドプロンプトを開きません。

ZIPには実行ファイルとドキュメント、第三者ライセンス通知だけを含めます。ユーザーのVault、データベース、認証情報、開発用チェックアウトは含めません。

### ソースから開発版を起動する

開発にはPython 3.10以上、Node.js/npm、Rust/Cargoが必要です。

```powershell
.\start-native.ps1
```

初回はフロントエンド依存関係をインストールしてTauri開発ウィンドウを起動します。ブラウザ互換のサーバーは次のように起動できます。

```powershell
python app.py
# http://127.0.0.1:8765 を開く
```

### Windowsアプリをビルドする

リポジトリのルートで実行します。

```powershell
.\build-native.ps1
.\package-release.ps1
```

Tauriのexeとインストーラーは `native/src-tauri/target/release/` に生成され、配布ZIPは `dist/` に生成されます。`package-release.ps1` がZIPへ入れるのはリリース用exe、ドキュメント、第三者ライセンス通知だけです。`dist/` とビルド生成物はGitで無視されます。

## 基本的な使い方

### 1. チャットから始める

アプリを開いて研究テーマ、問い、または作業内容を入力します。サイドバーでプロジェクトを選択するか、`Research OS`で新規プロジェクトを作ります。通常のメッセージは、選択中プロジェクトのContext Packetとともに設定済みのローカルエージェントへ渡されます。

会話はローカルに保存されます。成功したCodexのバナーなどの標準出力はチャット本文には表示されず、失敗時だけ問題解決に必要な診断情報を表示します。

### 2. 残すべき知見を保存する

通常の会話でも、回答後に設定済みエージェントが「このプロジェクトにとって長期的に残す価値があるか」を判断します。Decision、Finding、Failure、Evidence、Procedureなどに当たる durable な知見だけを、内部の構造化ブロックで自動保存します。挨拶、通常の進捗、一時的な提案、未検証の推測は保存しません。内部ブロックは回答を表示する前に取り除かれます。特定の内容を必ず保存したい場合はスラッシュコマンドを使い、Research StateやMemoryを画面で編集したい場合はチャットとは別の`Research OS`管理画面を使います。ローカルコマンドは外部エージェントを起動しません。

`タイトル | 本文` の形式では、最初の `|` より前がタイトル、後ろが本文になります。`|` がなければ先頭120文字がタイトルになります。

### 3. Research OS管理画面を使う

チャットと分離した管理画面には、プロジェクト選択・作成・完全削除、Research State編集、Memoryの作成・編集・削除・絞り込み、検索、状態別件数、Context Packet確認・コピー、グラフ確認、Maintenance、Obsidian同期、タグ付きノート取り込み、PDF Libraryがあります。

### PDF Library

管理画面のPDF LibraryでPDFを選び、研究分野を入力して追加します。PDFは現在のVaultの`Research Companion/PDF Library/<分野>/`へ保存され、同じSHA-256のPDFは同一プロジェクトへ重複登録しません。文字を持つPDFは抽出テキストを使い、画像PDFはRapidOCRの小型ONNX Runtime CPUモデルでOCRを試みます。GPUは必要ありません。

カードを開き「レイアウト翻訳PDFを生成」を押すと、ページ内の文字領域だけを翻訳し、同じ矩形へ日本語を配置した別PDFを作ります。図・表・段組み・ページ寸法は原PDFから引き継ぎ、数式らしい文字領域は原文を保持します。翻訳文の作成は設定済みのローカルエージェント（初期値はCodex）に任せ、PDFの再生成はローカルのレイアウトエンジンが行います。原PDFは上書きしません。画像PDFはCPU OCRの領域情報を使える場合に対応しますが、OCRが不完全な領域は原文のまま残ります。長い日本語は元の矩形内に収まるよう縮小されるため、生成後の目視確認を推奨します。

### 4. Obsidianで読む

VaultをObsidianで開き、最初に `Research Companion/Home.md` を開いてください。生成ページは標準の `[[wiki link]]` で接続されるため、Dataviewなどのプラグインは不要です。

```text
<vault>/
├─ Research Companion.md
└─ Research Companion/
   ├─ Home.md
   ├─ Projects/<project>/Project Overview.md
   ├─ Projects/<project>/Project State.md       # 互換リンク
   ├─ Projects/<project>/Memory Index.md
   ├─ Projects/<project>/Memory/<type>/*.md
   ├─ Conversations/*.md
   └─ Projects/<project>/Papers/<discipline>/*.md
```

Homeからプロジェクトへ移動できます。Project OverviewからMemory Indexと会話へ移動でき、Memoryからプロジェクト、関連Memory、元会話へ戻れます。同期時、ユーザーが編集した生成ファイルは上書きせず、隣に`Research Companion update`ファイルを作ります。

## スラッシュコマンド

入力欄に `/` を入力すると、候補・説明・使用例がその場で表示されます。クリック、またはTab/Enterで先頭候補を選択できます。`/help` も残っています。

| コマンド | 用途 | 例 |
| --- | --- | --- |
| `/help` | コマンド一覧 | `/help` |
| `/remember` | 一般メモを保存 | `/remember 共有された結果` |
| `/decision` | Decisionを保存 | `/decision SQLiteを使う \| 機械側の正本にする` |
| `/failure` | Failureを保存 | `/failure import失敗 \| UTF-8ではない入力だった` |
| `/question` | 未解決の問いを保存 | `/question Xは再現率を上げるか` |
| `/hypothesis` | 仮説を保存 | `/hypothesis Xは再現率を上げる` |
| `/evidence` | 根拠・観測結果を保存 | `/evidence ベンチマーク \| 5回中4回改善` |
| `/finding` | 分かったことを保存 | `/finding parserはShift-JISを拒否する` |
| `/experiment` | 実験を保存 | `/experiment 2つの検索方式を比較` |
| `/procedure` | 再現手順を保存 | `/procedure importテストを実行` |
| `/objective` | 研究目標を確認・更新 | `/objective 安定したimport経路を作る` |
| `/status` | Research Stateと件数を確認 | `/status` |
| `/search` | 知識ベースを検索 | `/search UTF-8 import` |
| `/context` | Agent Context Packetを表示 | `/context` |
| `/sync` | Vault全体を再生成 | `/sync` |
| `/import` | タグ付きユーザーノートを取り込む | `/import` |
| `/forget` | Memoryの状態を整理 | `/forget` |
| `/consolidate` | 関連Memoryを統合 | `/consolidate` |

未知のスラッシュコマンドは、設定したカスタムエージェントへそのまま渡されます。任意エージェントの独自ワークフローも利用できます。

## エージェントとWorking Directory

初期設定のコマンドは次のとおりです。

```text
codex exec --skip-git-repo-check --model gpt-5.6-luna -c model_reasoning_effort=low
```

Settingsの`Agent working directory`は、エージェントが起動する基準フォルダです。相対パス、検索、ファイル編集はこのフォルダを基準に行われます。作業させたいプロジェクトのルートを指定してください。これはVaultの場所とは別物です。

コマンドに `{prompt}` を含めるとプロンプトを1つの引数として渡し、含めない場合は標準入力へ渡します。

```text
codex exec {prompt}
claude -p {prompt}
```

アプリは作業ディレクトリを勝手に決めません。Settingsで明示的に選びます。指定したエージェントは現在のユーザー権限でファイルを読み書きできるため、慣れていないエージェントの検証には専用のチェックアウトを使ってください。

## データの場所

初期パスは実行時にOSのユーザー単位アプリデータ領域から決まり、開発者のマシンの絶対パスはソースに埋め込みません。現在使っている正確なVaultパスはSettingsとResearch OS画面に表示されます。

- 開発サーバー: 配布版と同じユーザー単位のデータ領域を使用。明示的に変える場合は `--db` または `RESEARCH_COMPANION_DATA_DIR` を指定
- Tauri配布版: ユーザー単位のアプリデータ領域にDB、workspace、初期Vaultを作成
- 任意の場所: SettingsでVault pathとAgent working directoryを変更。ブラウザ互換サーバーは `--db` でDBを変更

アプリDBが正本です。VaultのMarkdownは生成コメント付きの投影で、`.research-companion-manifest.json`で追跡します。ユーザーが書いたノートは、対応する`research-companion`のfrontmatter/tagが付いている場合だけ明示的に取り込みます。

## ローカルAPI

`python app.py`で起動し、`http://127.0.0.1:8765`を開きます。変更系リクエストには `X-Research-Companion: desktop` ヘッダーが必要です。これにより、無関係なローカルWebページがCSRF経由でAgent runnerを呼び出すのを防ぎます。

```powershell
$headers = @{'X-Research-Companion'='desktop'}
$project = Invoke-RestMethod http://127.0.0.1:8765/api/projects -Method Post `
  -Headers $headers -ContentType 'application/json' `
  -Body '{"name":"My Research","current_objective":"Test a hypothesis"}'

Invoke-RestMethod http://127.0.0.1:8765/api/context/compile -Method Post `
  -Headers $headers -ContentType 'application/json' `
  -Body (@{project_id=$project.id; query='retrieval'} | ConvertTo-Json)
```

## 開発・テスト

バックエンドの基本機能はPython標準ライブラリで動作します。PDF機能には`requirements.txt`のpypdf、pypdfium2、RapidOCR、ONNX Runtime CPUが必要です。ビルドスクリプトが自動インストールし、PyInstallerでバックエンドへ同梱します。

```powershell
python -m unittest discover -s tests -v
node --check static/app.js
python -m py_compile app.py
git diff --check
```

ネイティブビルドではPyInstallerでバックエンドをまとめ、Tauriでデスクトップシェルを作ります。`native/node_modules`、PyInstaller作業ファイル、Tauriのtarget、DB、キャッシュ、Vault内容、リリースZIPは無視対象で、コミットしてはいけません。第三者コンポーネントの扱いは`THIRD_PARTY_NOTICES.md`を確認してください。

## 安全性と設計上の境界

- SQLite、バックエンド、Tauri画面、初期Vaultはすべて原則ローカルで動作します。
- 外部エージェントコマンドは設定可能で、現在のユーザーのファイル権限を持ちます。
- APIキー、DB、会話ログ、生成Vault、個人パスはGitへ入れません。
- ローカル埋め込みは依存関係なしの決定的な代替実装であり、大規模モデルと同等の意味理解を保証しません。
- Maintenanceは初期状態では明示実行です。オプションのスケジューラーも研究上の事実を生成しません。
- Obsidianは正本ではありません。生成ページを直接編集しても保持され、後の同期では隣に更新案が作られます。ユーザーノートは生成フォルダの外に置いて明示的に取り込んでください。

## 現在の制限

組み込みのクラウド同期、複数ユーザーアカウント、Web自動調査クローラーはありません。エージェントの動作は設定したローカルCLIと、そのCLIの認証状態に依存します。ブラウザUIは互換性のため残していますが、エンドユーザー向けの推奨経路はTauriデスクトップアプリです。
