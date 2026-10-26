# AI enrichment and docs ingestion

AI is optional: generating, running, testing and repairing connectors need no model. A model can
fill gaps in a spec, build a connector from docs pages, and diagnose rejected requests during
[repair](repair.md).

## Choosing a model

Choose a model with `--model provider:model` (or set `CONNECTORGEN_MODEL`) and set that provider's key:

| `--model` | Provider | Key |
|---|---|---|
| `claude-sonnet-5-5` (default), `anthropic:claude-haiku-5-5` | Anthropic (needs the `[ai]` extra) | `ANTHROPIC_API_KEY` |
| `openai:gpt-6-luna` | OpenAI (Responses API) | `OPENAI_API_KEY` |
| `openrouter:org/model` | OpenRouter | `OPENROUTER_API_KEY` |
| `groq:…`, `together:…`, `mistral:…`, `deepseek:…`, `gemini:…` | OpenAI-compatible endpoints | `GROQ_API_KEY`, `TOGETHER_API_KEY`, … |
| `ollama:llama3.1:8b`, `lmstudio:…` | Local models, no key | none |
| `my-model --llm-base-url http://host:8000/v1` | Any OpenAI-compatible server (vLLM, LiteLLM, …) | `CONNECTORGEN_LLM_API_KEY` if needed |

A provider's own key is only sent to that provider's own endpoint: with `--llm-base-url`
pointing elsewhere, `OPENAI_API_KEY` and friends are ignored and only
`CONNECTORGEN_LLM_API_KEY` (or `api_key=` in code) is used.

Providers differ in structured-output support, so connectorgen asks for strict JSON Schema
output first and steps down automatically (JSON Schema → JSON mode → schema in the prompt).
Every reply is validated against the schema, with one repair attempt, so smaller and local
models work too. Proposals still have to pass the same contract tests whichever model made them.

## Enrich a generated connector

```bash
connectorgen enrich github.json --dry-run      # show proposals, write nothing
connectorgen enrich github.json -o github.enriched.json
connectorgen enrich api.json --passes auth --docs https://example.com/docs/auth
```

| Pass | Fills | Accepted only if |
|---|---|---|
| `pagination` | pagination the parser flagged in `action.notes` | params exist, shape is coherent, confidence isn't low, **and the action's contract test passes** |
| `auth` | auth when the spec declares none (GitHub's doesn't) | confidence isn't low, schemes are complete, **and contract tests pass with it** |
| `describe` | missing summaries/descriptions; better names for actions the parser named from the path | names are valid snake_case and unique |

Rules the passes follow:
- **The model proposes, contract tests decide.** Rejected proposals are listed with the reason.
- **The spec wins.** Values the spec provides are never overwritten.
- **No surprise renames.** Names from the spec's `operationId` stay unless you pass
  `--rename-all`, since renaming breaks anyone already using those actions.
- **Provenance.** Every model-made value is tagged with the model, e.g.
  `action.provenance == {"pagination": "llm:gpt-6-luna"}`.
- **Cached.** Responses are cached per model and endpoint in your user cache directory
  (`~/.cache/connectorgen/llm`, or `CONNECTORGEN_CACHE_DIR`), so re-runs only pay for what
  changed (`--no-cache` to bypass). Cached answers are re-validated before use.

Estimated cost of all three passes on GitHub's full spec (1,242 actions, 44 model calls,
~81K input / ~11K output tokens): about $0.27 at $2/$10 per million tokens, or about a cent with
small models priced around $0.10/$0.50. It's free with a local model.

## Build a connector from docs (no OpenAPI spec)

```bash
connectorgen from-docs https://docs.example.com/api/ --crawl --max-pages 20 -o example.json
connectorgen from-docs ./api-docs.html --base-url https://api.example.com -o example.json
```

Docs are untrusted input, and models can invent things, so the pipeline has guards:
- **Grounding:** an endpoint is kept only if every literal part of its path appears in the
  docs text. Invented endpoints are dropped and listed.
- **Base URL:** a model-provided base URL is used only if it's clearly the docs owner's own
  API host, over https on the default port: the docs host itself, or, for docs on a company's
  own domain (`acme.com`, `docs.acme.com`, `developer.acme.com`), the bare domain or
  `api.acme.com`. Anything else (other subdomains, which on multi-tenant platforms customers
  choose themselves; IP addresses; docs on hosting platforms like `acme.readme.io`) is left out
  with a note, so a malicious page can't steer your credentials to another host. Then, or for
  local files, pass `--base-url`.
- **Fetching:** only the sites you passed are fetched, redirects included, so a page can't point
  the crawler at internal addresses. At most `--max-pages` pages are fetched, each capped in size.
- **Destructive endpoints:** the model names them, so the flag is conservative and best-effort:
  every PUT, PATCH and DELETE; any POST whose summary starts with a destructive verb or says
  "permanently"/"cannot be undone", or whose name or path has one (delete, reset, prune, revoke,
  refund, merge, ...) unless it clearly only reads (`.../status`, "Get ...", "Validate ...");
  and generic executors (SQL, GraphQL, RPC). `serve` and `call` hide or refuse destructive
  actions unless allowed; check `inspect` and adjust `destructive` in the file if needed.
- **Schemas:** model-written JSON Schemas must be valid, or they're dropped with a note.
- **Prompts:** docs are wrapped in `<docs>` tags and the model is told to treat them as data.
- The result is contract-tested before it's written.

Docs sites that render everything with JavaScript return little text to a plain fetch; save the
rendered page as HTML and pass the file instead.

## From Python

```python
from connectorgen.enrich import enrich
from connectorgen.ingest.docs import connector_from_docs
from connectorgen.llm import CachedLLM, llm_from_spec

llm = CachedLLM(llm_from_spec("openai:gpt-6-luna"))   # or "claude-sonnet-5-5", "ollama:llama3.1", ...
enriched, report = enrich(connector, llm, passes=["pagination", "auth", "describe"])
print(report.to_text())

connector, docs_report = connector_from_docs(["https://docs.example.com/api/"], llm, crawl=True)
```

Any object with `model`, `usage` and `complete_json(system=, prompt=, schema=, max_tokens=)`
works as the model (see `connectorgen.llm.LLM`), so your app can plug in its own model gateway.
`FakeLLM` scripts responses for tests and checks they match the schema.

[← Back to the README](../README.md)
