# Research Companion OS

Research Companion OS is a local-first research continuity layer for Codex and other replaceable AI agents. It stores project state, decisions, failures, questions, hypotheses, evidence, findings, skills, and provenance in SQLite, then compiles a small Agent Context Packet instead of injecting an entire chat history.

## Run

Requires Python 3.10+ and no third-party package.

```powershell
python app.py
```

Open <http://127.0.0.1:8765>. The database is created as `research_companion.db` on first run. Use `--db` to place it elsewhere.

The main experience is a ChatGPT-style chat. A shared Obsidian Vault is created automatically at `./vault` and every conversation is written to `vault/Conversations`; explicitly marked insights are also projected into the research memory folders. Use `/remember`, `/decision`, `/failure`, `/question`, `/evidence`, or `/finding` at the start of a message to save a durable insight. The Vault path can be changed in Settings.

To run an existing local agent, open Settings and set its working directory and command. For example, set the directory to the project you want the agent to work in and use `codex exec {prompt}` or `claude -p {prompt}`. If `{prompt}` is present, the prompt is passed as one argument; otherwise it is sent over standard input. An empty command keeps the app in local memory/transcript mode.

## Implemented capabilities

- Research State: objective, blockers, questions, hypotheses, assumptions, actions, risks
- First-class memory types: Decision, Failure, Question, Evidence, Hypothesis, Finding, Experiment, Procedure, and more
- Provenance and confidence on every memory
- Hybrid retrieval: SQLite FTS5 BM25 + deterministic local embeddings + project/utility/temporal/graph scoring
- Evidence Graph relations and related-memory edges
- Context Compiler endpoint: `POST /api/context/compile`
- Reversible forgetting lifecycle: `HOT → WARM → COLD → ARCHIVED`
- Rule-based consolidation that archives originals and keeps traceable consolidated records
- Obsidian projection: `POST /api/obsidian/sync`
- Background scheduler for hourly, daily, and weekly maintenance
- Responsive browser UI for capture, recall, state inspection, context compilation, and maintenance

## API examples

```powershell
$project = Invoke-RestMethod http://127.0.0.1:8765/api/projects -Method Post -ContentType 'application/json' -Body '{"name":"My Research","current_objective":"Test a hypothesis"}'

Invoke-RestMethod http://127.0.0.1:8765/api/memories -Method Post -ContentType 'application/json' -Body (@{
  project_id=$project.id; type='decision'; title='Use SQLite'; content='SQLite is the machine source of truth.';
  source_type='direct_user_statement'; importance=.9; rediscovery_cost=.9
} | ConvertTo-Json)

Invoke-RestMethod http://127.0.0.1:8765/api/context/compile -Method Post -ContentType 'application/json' -Body (@{project_id=$project.id; query='retrieval'} | ConvertTo-Json)
```

## Design boundary

The local embedding is a deterministic zero-dependency fallback, not a claim of semantic parity with a large embedding model. The `memory_embeddings` table and scoring boundary are intentionally isolated so a future local `sentence-transformers` adapter can replace it without changing the rest of the system. The server does not invent research facts in background jobs; autonomous work is limited to maintenance and review until an agent adapter is explicitly connected.
