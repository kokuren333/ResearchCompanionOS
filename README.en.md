# Research Companion OS — English Guide

[日本語版 / Japanese](README.ja.md) · [Project overview](README.md)

Research Companion OS is a local-first research continuity tool. Its normal interface is a ChatGPT-style chat. The app keeps durable research state and structured memories in SQLite, compiles a small context packet for an agent, and projects the resulting knowledge into a navigable Obsidian vault. Research PDFs can be stored by discipline and exported as separate layout-preserving Japanese PDFs.

## Why it exists

Long-running research loses context when every session starts from an empty chat. Research Companion separates the durable record from the current conversation:

1. You work in the chat.
2. Important decisions, failures, questions, evidence, and findings are captured as typed Memory records.
3. Search and Context Packet compilation select only relevant knowledge for the next agent run.
4. Obsidian receives a human-readable projection with links between projects, memories, and source conversations.

SQLite is the source of truth. Obsidian is a readable projection and an optional place for tagged notes to be imported.

## Quick start on Windows

### Use a release ZIP

Unzip the release and launch `Research Companion.exe`. The Python backend is bundled inside the Tauri application, so Python is not required to run the packaged app. The backend listens only on a dynamically allocated localhost port and the Windows build does not open a console window.

The ZIP is intentionally limited to the executable, documentation, and third-party notices. It does not contain a user Vault, database, credentials, or development checkout.

### Run from source

For development, install Python 3.10+, Node.js/npm, and Rust/Cargo:

```powershell
.\start-native.ps1
```

The first run installs frontend dependencies and starts the Tauri development window. A browser-compatible backend is also available:

```powershell
python app.py
# open http://127.0.0.1:8765
```

### Build the Windows app

From the repository root:

```powershell
.\build-native.ps1
.\package-release.ps1
```

Tauri installers and the executable are written under `native/src-tauri/target/release/`. The packaging script creates a ZIP under `dist/` and includes only the release executable, documentation, and third-party license notices. `dist/` and all build outputs are ignored by Git.

## The normal workflow

### 1. Start with Chat

Open the app and type a research question or objective. Select a project from the sidebar, or create one in `Research OS`. A normal message is sent to the configured local agent with a context packet assembled from the selected project.

The app saves the conversation locally. Successful agent diagnostics such as the Codex banner are not shown as chat content; diagnostics are retained only when a command fails, where they are useful for troubleshooting.

### 2. Capture durable knowledge

Normal chat is also eligible for automatic knowledge capture. After answering, the configured agent judges whether the exchange contains a durable project-specific insight. It silently emits a structured capture block only for useful decisions, findings, failures, evidence, procedures, or similar knowledge; greetings, routine progress, temporary suggestions, and unverified speculation are not saved as Memory. The protocol is removed before the answer is shown. Use a slash command when you want to force a specific capture, or use the separate `Research OS` management view to edit Research State and Memory records. Local commands do not start an external agent.

When a command accepts `Title | content`, the part before the first `|` becomes the title and the rest becomes the content. Without `|`, the first 120 characters are used as the title.

### 3. Review in Research OS

`Research OS` is deliberately separate from Chat. It contains project selection, creation and permanent deletion, Research State editing, Memory CRUD and filtering, hybrid search, state counts, Context Packet preview/copy, graph inspection, maintenance, Obsidian sync, tagged-note import, and a PDF Library.

### PDF Library

Use the PDF Library card to select a PDF and enter a research discipline. The original is stored under the active Vault at `Research Companion/PDF Library/<discipline>/`; an SHA-256 duplicate is not registered twice in the same project. Text PDFs use extracted text. Image-only pages are sent through RapidOCR's small ONNX Runtime CPU model when available, so an NVIDIA GPU is not required.

Open a paper card and choose `レイアウト翻訳PDFを生成` to create a separate Japanese PDF. The configured local agent (Codex by default) translates detected text blocks, and the local PDF writer removes only those text objects and inserts the translation into the same rectangles. Figures, tables, columns, page dimensions, and formula-like text are retained from the original. The original PDF is never overwritten. Scanned pages use OCR regions when available; uncertain or missing OCR regions remain unchanged. Long translations are reduced to fit the source rectangle, so review the generated PDF. Summarisation still uses the configured agent and is saved as a Finding Memory and Paper note in Obsidian.

### 4. Browse the Obsidian projection

Open the Vault folder in Obsidian and start at `Research Companion/Home.md`. The generated pages use standard `[[wiki links]]`; Dataview or another plugin is not required.

Typical structure:

```text
<vault>/
├─ Research Companion.md
└─ Research Companion/
   ├─ Home.md
   ├─ Projects/<project>/Project Overview.md
   ├─ Projects/<project>/Project State.md       # compatibility link
   ├─ Projects/<project>/Memory Index.md
   ├─ Projects/<project>/Memory/<type>/*.md
   ├─ Conversations/*.md
   └─ Projects/<project>/Papers/<discipline>/*.md
```

Home links to projects. A project overview links to its Memory Index and conversations. Memory pages link to their project, related memories, and source conversation. A full sync preserves generated files that a user has edited and writes a separate `Research Companion update` file instead of silently overwriting them.

## Slash commands

Type `/` in the composer. The suggestion list appears immediately with a description and example. Click a command, or use Tab/Enter to select the first candidate. `/help` remains available.

| Command | Purpose | Example |
| --- | --- | --- |
| `/help` | Show the command list | `/help` |
| `/remember` | Save a general note | `/remember Shared result` |
| `/decision` | Save a decision | `/decision Use SQLite \| It is the source of truth` |
| `/failure` | Save a failure | `/failure Import failed \| The note had invalid UTF-8` |
| `/question` | Save an open question | `/question Does X improve recall?` |
| `/hypothesis` | Save a hypothesis | `/hypothesis X improves recall` |
| `/evidence` | Save evidence or an observation | `/evidence Benchmark log \| 4/5 runs improved` |
| `/finding` | Save a finding | `/finding The parser rejects Shift-JIS` |
| `/experiment` | Save an experiment | `/experiment Compare two retrievers` |
| `/procedure` | Save a reproducible procedure | `/procedure Run the import test` |
| `/objective` | Read or update the project objective | `/objective Build a reliable import path` |
| `/status` | Show current Research State and counts | `/status` |
| `/search` | Search the project knowledge base | `/search UTF-8 import` |
| `/context` | Display the compiled Agent Context Packet | `/context` |
| `/sync` | Regenerate the complete Vault projection | `/sync` |
| `/import` | Import tagged user-authored Obsidian notes | `/import` |
| `/forget` | Apply reversible Memory lifecycle rules | `/forget` |
| `/consolidate` | Consolidate related Memory records | `/consolidate` |

Unknown slash commands are still passed to the configured custom agent, so custom agent workflows remain possible.

## Agents and working directories

The default command is:

```text
codex exec --skip-git-repo-check --model gpt-5.6-luna -c model_reasoning_effort=low
```

In Settings, `Agent working directory` is the folder in which the agent starts. Relative paths, searches, and file edits are based there. Set it to the root of the project you want the agent to work on; it is not the Vault path. The command may contain `{prompt}` to pass the prompt as one argument. If it does not, the prompt is sent through standard input.

Examples:

```text
codex exec {prompt}
claude -p {prompt}
```

The app does not invent a project directory. Choose it explicitly in Settings. The configured agent may read and modify files there with the permissions of the current user, so use a dedicated checkout when testing unfamiliar agents.

## Data locations

The default path is calculated at runtime from the operating system's per-user application-data directory; no developer-machine absolute path is embedded in the source. The exact active Vault path is displayed in Settings and in the Research OS view.

- Development server: defaults to the same per-user data directory; use `--db` or `RESEARCH_COMPANION_DATA_DIR` for an explicit location.
- Packaged Tauri app: uses its per-user application-data directory for the database, workspace, and default Vault.
- Custom locations: set `Vault path` and `Agent working directory` in Settings, or use `--db` for the browser-compatible server.

The application database is authoritative. Vault Markdown files are generated projections marked with a generated-file comment and tracked by `.research-companion-manifest.json`. User-authored notes are imported only when they carry the supported `research-companion` frontmatter/tag conventions. PDF originals are user research assets under the Vault's PDF Library and are not generated Markdown projections.

## Local API

Start `python app.py` and open `http://127.0.0.1:8765`. Core features use the Python standard library; the PDF feature requires the packages in `requirements.txt`. Mutating requests require `X-Research-Companion: desktop`; this prevents an unrelated local web page from invoking the agent runner through CSRF.

```powershell
$headers = @{'X-Research-Companion'='desktop'}
$project = Invoke-RestMethod http://127.0.0.1:8765/api/projects -Method Post `
  -Headers $headers -ContentType 'application/json' `
  -Body '{"name":"My Research","current_objective":"Test a hypothesis"}'

Invoke-RestMethod http://127.0.0.1:8765/api/context/compile -Method Post `
  -Headers $headers -ContentType 'application/json' `
  -Body (@{project_id=$project.id; query='retrieval'} | ConvertTo-Json)
```

## Development and tests

The core backend uses the Python standard library; the PDF reader adds the pinned packages in `requirements.txt`. Run:

```powershell
python -m unittest discover -s tests -v
node --check static/app.js
python -m py_compile app.py
git diff --check
```

The native build uses PyInstaller for the backend and Tauri for the desktop shell. `native/node_modules`, PyInstaller work files, Tauri targets, databases, caches, Vault contents, and release ZIPs are ignored and must not be committed.

## Security and design boundaries

- Everything is local by default: SQLite, the backend, the Tauri window, and the default Vault.
- The external agent command is user-configurable and can have the user's filesystem permissions.
- No API key, database, chat transcript, generated Vault, or personal path belongs in Git.
- The deterministic local embedding is a zero-dependency fallback, not a claim of parity with a large semantic model.
- Maintenance is explicit by default; the optional scheduler does not invent research facts.
- Obsidian is not the source of truth. Editing generated pages directly is preserved; a later sync writes a separate update file. Put durable user-authored notes outside generated folders and import them explicitly.

## Current limitations

There is no built-in cloud synchronization, multi-user account system, or automatic web research crawler. Agent behavior depends on the configured local CLI and its own authentication. The browser UI remains for compatibility, but the supported end-user path is the Tauri desktop app.
