# Research Companion OS

> ローカルで動く、研究の継続性を保つためのChatGPT風デスクトップアプリ。

| Language | Documentation |
| --- | --- |
| 日本語 | [README.ja.md](README.ja.md) |
| English | [README.en.md](README.en.md) |

## これは何か

Research Companion OSは、Codexなどのローカルエージェントと長期研究を続けるためのローカルファーストな記憶・状態管理基盤です。普段はチャットだけを使い、必要な知見をSQLiteに保存します。保存したMemoryは検索・Context Packet・Obsidianの読みやすい知識ベースに変換できます。

主な特徴:

- ChatGPTに近いチャット中心のデスクトップUI
- Research State、Decision、Failure、Question、Evidence、Findingなどの構造化された知識
- SQLite FTS5とローカル埋め込みを組み合わせた検索
- プロジェクト、Memory、会話、関連リンクを辿れるObsidian出力
- `/` を入力した時点で候補と使い方を表示するスラッシュコマンド
- Codex、Claude、その他の任意のローカルエージェントを指定ディレクトリで実行
- Pythonバックエンドを同梱したTauri Windowsアプリ

## 詳しいドキュメント

- [日本語ガイド](README.ja.md)
- [English guide](README.en.md)

ソースから開発版を起動するには `.\start-native.ps1`、Windowsアプリをビルドするには `.\build-native.ps1`、配布ZIPを作るには `.\package-release.ps1` を使います。

リポジトリにはアプリのソースとビルド設定だけを置きます。ユーザーのSQLite、会話ログ、Obsidian Vault、`node_modules`、Tauriのビルドキャッシュは含めません。ビルド済みZIPはGitHub Releaseの配布物として扱います。
