"""Command line: `connectorgen generate | inspect | call`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from .errors import ConnectorError
from .ingest.openapi import connector_from_openapi
from .ir import Connector
from .runtime.auth import Credentials
from .runtime.client import ConnectorClient

app = typer.Typer(help="Generate API connectors from OpenAPI specs and run them.",
                  no_args_is_help=True)

_MODEL_HELP = ("Model as provider:model, e.g. claude-sonnet-5-5, openai:gpt-6-luna, "
               "openrouter:org/model, ollama:llama3.1 (default: $CONNECTORGEN_MODEL or claude-sonnet-5-5).")
_LLM_URL_HELP = "Base URL of any OpenAI-compatible server (vLLM, LiteLLM, ...)."


@app.command()
def generate(
    source: str = typer.Argument(..., help="OpenAPI 3.x spec: URL or file path (JSON/YAML)."),
    output: Path = typer.Option(Path("connector.json"), "-o", "--output"),
    name: Optional[str] = typer.Option(None, help="Connector name (default: from spec title)."),
    base_url: Optional[str] = typer.Option(None, help="Override the spec's server URL."),
) -> None:
    """Generate a connector spec from an OpenAPI document."""
    try:
        connector = connector_from_openapi(source, name=name, base_url=base_url)
    except ConnectorError as e:
        typer.secho(f"Error: {e}", fg="red", err=True)
        raise typer.Exit(1)
    connector.save(output)
    paginated = sum(a.pagination.style != "none" for a in connector.actions)
    flagged = [a for a in connector.actions if a.notes]
    polling = sum(t.kind == "polling" for t in connector.triggers)
    hooks = len(connector.triggers) - polling
    typer.echo(f"Wrote {output}: {len(connector.actions)} actions "
               f"({paginated} paginated), {polling} polling + {hooks} webhook triggers, auth: "
               f"{', '.join(s.type for s in connector.auth) or 'none'}")
    if not connector.base_url:
        typer.secho("Warning: no server URL in spec; pass --base-url.", fg="yellow")
    for note in connector.notes:
        typer.secho(f"Note: {note}", fg="yellow")
    if flagged:
        typer.secho(f"{len(flagged)} action(s) have notes needing review "
                    "(see `connectorgen inspect`).", fg="yellow")


@app.command()
def convert(
    source: str = typer.Argument(..., help="Swagger 2.0 spec: URL or file path (JSON/YAML)."),
    output: Path = typer.Option(Path("openapi.yaml"), "-o", "--output",
                                help="Write .yaml or .json (by extension)."),
) -> None:
    """Convert a Swagger 2.0 spec to OpenAPI 3.0 (generate does this automatically)."""
    import yaml

    from .ingest.openapi import fetch_spec_text, parse_spec_text
    from .ingest.swagger2 import convert_swagger2

    try:
        if source.startswith(("http://", "https://")):
            text = fetch_spec_text(source)
        else:
            text = Path(source).read_text(encoding="utf-8", errors="replace")
        converted = convert_swagger2(parse_spec_text(text))
    except ConnectorError as e:
        typer.secho(f"Error: {e}", fg="red", err=True)
        raise typer.Exit(1)
    if output.suffix == ".json":
        output.write_text(json.dumps(converted, indent=2) + "\n")
    else:
        output.write_text(yaml.safe_dump(converted, sort_keys=False, allow_unicode=True))
    typer.echo(f"Wrote {output}: OpenAPI 3.0.3, {len(converted.get('paths', {}))} paths")


@app.command()
def inspect(connector_path: Path = typer.Argument(..., help="Generated connector.json")) -> None:
    """List a connector's actions."""
    c = Connector.load(connector_path)
    where = f"MCP {c.mcp.transport}: {c.mcp.describe()}" if c.mcp else f"base: {c.base_url or '(none)'}"
    typer.echo(f"{c.title} ({c.name}) v{c.api_version}  {where}")
    if c.mcp and c.mcp.env_passthrough:
        typer.echo(f"  needs env: {', '.join(c.mcp.env_passthrough)}")
    for a in c.actions:
        flags = []
        if a.pagination.style != "none":
            flags.append(f"paginated:{a.pagination.style}")
        if a.destructive:
            flags.append("destructive")
        required = [p.name for p in a.params if p.required] + (["body"] if a.body_required else [])
        if a.arguments_schema is not None:
            required = list(a.arguments_schema.get("required") or [])
        verb = ("read" if a.safe else "write") if a.mcp_tool else a.method
        line = f"  {verb:<6} {a.name:<32} {a.path or ('tool ' + a.mcp_tool if a.mcp_tool else '')}"
        if required:
            line += f"  required: {', '.join(required)}"
        if flags:
            line += f"  [{', '.join(flags)}]"
        typer.echo(line)
        for note in a.notes:
            typer.secho(f"         note: {note}", fg="yellow")
    for s in c.auth:
        if s.oauth_flows:
            flows = ", ".join(f.flow for f in s.oauth_flows)
            typer.echo(f"  auth {s.id}: oauth2 ({flows})")
    if c.triggers:
        typer.echo(f"Triggers ({len(c.triggers)}):")
        for t in c.triggers:
            if t.kind == "polling":
                extra = f"polls {t.source_action}, dedup by {t.dedup_key or 'hash'}, order {t.order}"
            else:
                extra = f"webhook event {t.event}" + (" (signed)" if t.signature else "")
            typer.echo(f"  {t.kind:<8} {t.name:<40} {extra}")


@app.command()
def test(
    connector_path: Path = typer.Argument(..., help="Generated connector.json"),
    action: list[str] = typer.Option([], "-a", "--action", help="Only these actions."),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="Show passing actions too."),
    emit_pytest: Optional[Path] = typer.Option(
        None, help="Also write a pytest file that runs these checks in CI."),
) -> None:
    """Run offline contract tests: every action against a schema-built mock server."""
    from .validate import run_contract_tests, write_pytest

    c = Connector.load(connector_path)
    report = run_contract_tests(c, action or None)
    typer.echo(report.to_text(verbose=verbose))
    if emit_pytest:
        write_pytest(connector_path, emit_pytest)
        typer.echo(f"Wrote {emit_pytest}")
    if not report.ok:
        raise typer.Exit(1)


def _parse_kv(items: list[str]) -> dict:
    out: dict = {}
    for item in items:
        key, sep, raw = item.partition("=")
        if not sep:
            raise typer.BadParameter(f"Expected key=value, got '{item}'")
        try:
            out[key] = json.loads(raw)
        except json.JSONDecodeError:
            out[key] = raw
    return out


def _default_token_file(c: Connector) -> Path:
    return Path(".connectorgen_tokens") / f"{c.name}.json"


def _credentials(c: Connector, token_file: Optional[Path]) -> Credentials:
    """Env credentials, plus an OAuth session when a saved token file is given.
    Refreshing needs CONNECTORGEN_CLIENT_ID (and _CLIENT_SECRET for confidential apps)."""
    import os

    creds = Credentials.from_env()
    if token_file is None:
        return creds
    from .runtime.oauth import FileTokenStore, OAuth2Session

    scheme = next((s for s in c.auth if s.type == "oauth2" and s.oauth_flows), None)
    if scheme is None:
        raise typer.BadParameter("--token-file given, but this connector has no OAuth2 flow.")
    if not token_file.exists():
        raise typer.BadParameter(f"{token_file} doesn't exist; run `connectorgen auth` first.")
    creds.oauth = OAuth2Session(scheme.oauth_flows[0],
                                client_id=os.environ.get("CONNECTORGEN_CLIENT_ID", ""),
                                client_secret=os.environ.get("CONNECTORGEN_CLIENT_SECRET"),
                                store=FileTokenStore(token_file))
    return creds


@app.command()
def auth(
    connector_path: Path = typer.Argument(...),
    client_id: str = typer.Option(..., envvar="CONNECTORGEN_CLIENT_ID"),
    client_secret: Optional[str] = typer.Option(None, envvar="CONNECTORGEN_CLIENT_SECRET",
                                                help="Omit for public (PKCE-only) apps."),
    scope: list[str] = typer.Option([], help="Scopes to request (default: all in the spec)."),
    flow: Optional[str] = typer.Option(None, help="authorization_code or client_credentials."),
    port: int = typer.Option(8765, help="Loopback port; register http://127.0.0.1:<port>/callback."),
    token_file: Optional[Path] = typer.Option(None, help="Default: .connectorgen_tokens/<name>.json"),
    no_browser: bool = typer.Option(False, help="Print the URL instead of opening a browser."),
) -> None:
    """Sign in with OAuth2 and save the token (refreshed automatically when used)."""
    from .runtime.oauth import FileTokenStore, OAuth2Session, OAuthError, run_loopback_flow

    c = Connector.load(connector_path)
    flows = [f for s in c.auth if s.type == "oauth2" for f in s.oauth_flows]
    if flow:
        flows = [f for f in flows if f.flow == flow]
    if not flows:
        typer.secho("This connector has no usable OAuth2 flow in its spec.", fg="red", err=True)
        raise typer.Exit(1)
    target = token_file or _default_token_file(c)
    session = OAuth2Session(flows[0], client_id, client_secret, store=FileTokenStore(target),
                            scopes=scope or None)
    try:
        if flows[0].flow == "client_credentials":
            session.fetch_client_credentials()
        else:
            run_loopback_flow(session, port=port, open_browser=not no_browser,
                              on_url=lambda u: typer.echo(f"Open this URL to sign in:\n{u}"))
    except OAuthError as e:
        typer.secho(f"Error: {e}", fg="red", err=True)
        raise typer.Exit(1)
    typer.echo(f"Signed in. Token saved to {target} (owner-only permissions).")


@app.command()
def poll(
    connector_path: Path = typer.Argument(...),
    trigger: str = typer.Argument(...),
    inputs: list[str] = typer.Option([], "-i", "--input", help="key=value for the list call"),
    state_dir: Path = typer.Option(Path(".connectorgen_state")),
    watch: bool = typer.Option(False, help="Keep polling every --interval seconds."),
    interval: float = typer.Option(60.0),
    emit_existing: bool = typer.Option(False, help="On first run, emit existing items too."),
    max_pages: int = typer.Option(10, help="Pages read per poll."),
    max_items: int = typer.Option(500, help="Items read per poll."),
    token_file: Optional[Path] = typer.Option(None),
    base_url: Optional[str] = typer.Option(None),
) -> None:
    """Run a polling trigger: print each new item as one JSON line."""
    import time as _time

    from .triggers import FileStateStore, Poller

    c = Connector.load(connector_path)
    try:
        client = ConnectorClient(c, _credentials(c, token_file), base_url=base_url)
        poller = Poller(client, trigger, FileStateStore(state_dir), inputs=_parse_kv(inputs),
                        emit_on_first_run=emit_existing, max_pages=max_pages,
                        max_items=max_items)
    except (ConnectorError, KeyError) as e:
        typer.secho(f"Error: {e}", fg="red", err=True)
        raise typer.Exit(1)
    try:
        while True:
            try:
                result = poller.poll()
            except ConnectorError as e:
                typer.secho(f"Poll failed: {e}", fg="red", err=True)
                if not watch:
                    raise typer.Exit(1)
            else:
                for item in result.items:
                    typer.echo(json.dumps(item))
                if result.baseline and not emit_existing:
                    typer.secho(f"Baseline recorded ({result.fetched} existing items); new items "
                                "will be emitted from the next poll.", fg="yellow", err=True)
                if not result.complete:
                    typer.secho(f"Only the first {result.fetched} items were read and the list "
                                "goes on; newer items past that point can't be seen. Raise "
                                "--max-pages, or use a trigger with a sort order.",
                                fg="yellow", err=True)
            if not watch:
                break
            _time.sleep(interval)
    finally:
        client.close()


@app.command()
def smoke(
    connector_path: Path = typer.Argument(...),
    action: list[str] = typer.Option([], "-a", "--action", help="Only these (GET) actions."),
    param: list[str] = typer.Option([], "--param", help="name=value for any action with that param"),
    max_actions: int = typer.Option(50),
    delay: float = typer.Option(0.2, help="Seconds between calls."),
    json_out: Optional[Path] = typer.Option(None, "--json", help="Write the report as JSON."),
    token_file: Optional[Path] = typer.Option(None),
    base_url: Optional[str] = typer.Option(None),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Call read-only GET actions against the live API and report errors and schema drift."""
    from .validate.smoke import run_smoke_tests

    c = Connector.load(connector_path)
    try:
        report = run_smoke_tests(c, _credentials(c, token_file), actions=action or None,
                                 global_params=_parse_kv(param), base_url=base_url,
                                 max_actions=max_actions, delay=delay)
    except ConnectorError as e:
        typer.secho(f"Error: {e}", fg="red", err=True)
        raise typer.Exit(1)
    typer.echo(report.to_text(verbose=verbose))
    if json_out:
        json_out.write_text(json.dumps(report.to_dict(), indent=2))
    if not report.ok:
        raise typer.Exit(1)


@app.command("from-mcp")
def from_mcp(
    command: Optional[str] = typer.Option(None, help='stdio server, e.g. "npx -y @scope/server".'),
    url: Optional[str] = typer.Option(None, help="Streamable HTTP endpoint, e.g. https://x/mcp."),
    output: Path = typer.Option(Path("connector.json"), "-o", "--output"),
    name: Optional[str] = typer.Option(None),
    env: list[str] = typer.Option([], "--env", help="Env var NAME the server needs; its value "
                                                    "is read from your environment at runtime."),
    header: list[str] = typer.Option([], "--header", help="Static, non-secret header K=V."),
    bearer: bool = typer.Option(False, help="HTTP: send CONNECTORGEN_TOKEN as a bearer token."),
    api_key_header: Optional[str] = typer.Option(None, help="HTTP: send CONNECTORGEN_API_KEY "
                                                            "in this header."),
    cwd: Optional[str] = typer.Option(None, help="stdio: working directory."),
    allow_secret_like_values: bool = typer.Option(
        False, help="Save values the secret check flagged, because you know they aren't secrets."),
) -> None:
    """Import an MCP server's tools as a connector (no secrets are written to the file)."""
    import shlex

    from .ir import AuthScheme, McpServerConfig
    from .mcp import connector_from_mcp

    if bool(command) == bool(url):
        raise typer.BadParameter("Give exactly one of --command or --url.")
    from .runtime.urls import secret_header_names

    headers = {k: str(v) for k, v in _parse_kv(header).items()}
    secretish = secret_header_names(headers)
    if secretish and not allow_secret_like_values:
        raise typer.BadParameter(f"Don't put credentials in --header ({', '.join(secretish)}); "
                                 "use --bearer or --api-key-header so they stay out of the file.")
    if command:
        parts = shlex.split(command)
        config = McpServerConfig(transport="stdio", command=parts[0], args=parts[1:],
                                 env_passthrough=env, cwd=cwd)
    else:
        config = McpServerConfig(transport="http", url=url, headers=headers)
    auth = []
    if bearer:
        auth.append(AuthScheme(id="bearer", type="bearer"))
    if api_key_header:
        auth.append(AuthScheme(id="api_key", type="api_key", param_name=api_key_header,
                               location="header"))
    try:
        connector = connector_from_mcp(config, name=name, auth=auth,
                                       credentials=Credentials.from_env(),
                                       allow_secret_like_values=allow_secret_like_values)
    except ConnectorError as e:
        typer.secho(f"Error: {e}", fg="red", err=True)
        raise typer.Exit(1)
    connector.save(output)
    reads = sum(a.safe for a in connector.actions)
    destructive = sum(a.destructive for a in connector.actions)
    typer.echo(f"Wrote {output}: {connector.title}, {len(connector.actions)} tools "
               f"({reads} read-only, {destructive} destructive)")


@app.command()
def serve(
    connector_path: Path = typer.Argument(...),
    transport: str = typer.Option("stdio", help="stdio or http."),
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(8000),
    action: list[str] = typer.Option([], "-a", "--action", help="Expose only these (globs ok)."),
    tag: list[str] = typer.Option([], "--tag", help="Expose only actions with these tags."),
    read_only: bool = typer.Option(False, help="Expose only read-only actions."),
    allow_destructive: bool = typer.Option(False, help="Also expose destructive actions."),
    token_file: Optional[Path] = typer.Option(None),
    base_url: Optional[str] = typer.Option(None),
) -> None:
    """Serve a connector as an MCP server, so Claude and other agents can use its actions."""
    import anyio

    from .mcp import build_mcp_server, serve_http, serve_stdio

    c = Connector.load(connector_path)
    try:
        server = build_mcp_server(c, _credentials(c, token_file), actions=action or None,
                                  tags=tag or None, read_only=read_only,
                                  allow_destructive=allow_destructive, base_url=base_url)
    except ConnectorError as e:
        typer.secho(f"Error: {e}", fg="red", err=True)
        raise typer.Exit(1)
    try:
        if transport == "stdio":
            anyio.run(serve_stdio, server)
        elif transport == "http":
            typer.secho(f"Serving MCP at http://{host}:{port}/mcp", fg="green", err=True)
            serve_http(server, host=host, port=port)
        else:
            raise typer.BadParameter("--transport must be stdio or http")
    finally:
        server.connectorgen_client.close()


EXIT_UPDATE = 10   # check: safe update available
EXIT_REVIEW = 20   # check: needs a person (breaking changes, live drift, unverified docs change)


@app.command()
def check(
    connector_path: Path = typer.Argument(..., help="Connector to check against its source."),
    out: Path = typer.Option(Path("repair"), "-o", "--out", help="Where to write the proposal."),
    spec: Optional[str] = typer.Option(None, help="Override the recorded spec path/URL."),
    smoke_report: Optional[Path] = typer.Option(None, help="JSON from `smoke --json` as evidence."),
    used: list[str] = typer.Option([], "--used", help="Actions your workflows use (repeatable)."),
    auto_apply: bool = typer.Option(False, help="Apply automatically if the verdict is safe_update."),
    model: Optional[str] = typer.Option(None, help="LLM (provider:model): regenerates docs-based "
                                                   "connectors and diagnoses rejected requests (--fix-live)."),
    llm_base_url: Optional[str] = typer.Option(None, envvar="CONNECTORGEN_LLM_BASE_URL", help=_LLM_URL_HELP),
    fix_live: bool = typer.Option(False, help="Propose fixes when the live API disagrees with an "
                                              "unchanged source (needs --smoke-report)."),
    verify_live: bool = typer.Option(False, help="Re-call the API (read-only) to confirm live fixes."),
    param: list[str] = typer.Option([], "--param", help="name=value for --verify-live calls "
                                                        "(same as given to smoke)."),
) -> None:
    """Re-read the source; if it changed, write a reviewed update proposal (never applies
    breaking changes on its own). Exit codes: 0 no change, 10 safe update, 20 needs review."""
    from .repair import apply_proposal, check_for_drift

    c = Connector.load(connector_path)
    try:
        llm = _make_llm(model, False, llm_base_url) if (model or llm_base_url) else None
    except ConnectorError as e:
        typer.secho(f"Error: {e}", fg="red", err=True)
        raise typer.Exit(1)
    try:
        if fix_live and smoke_report is None:
            raise typer.BadParameter("--fix-live needs --smoke-report (from `smoke --json`).")
        proposal = check_for_drift(c, spec=spec, credentials=Credentials.from_env(), llm=llm,
                                   smoke_report=smoke_report, used_actions=used,
                                   current_path=connector_path, fix_live=fix_live,
                                   verify_live=verify_live, global_params=_parse_kv(param))
    except ConnectorError as e:
        typer.secho(f"Error: {e}", fg="red", err=True)
        raise typer.Exit(1)
    typer.echo(proposal.to_text())
    if proposal.verdict == "no_change":
        return
    proposal.save(out)
    typer.echo(f"\nProposal written to {out}/ (summary.txt, proposal.json"
               f"{', candidate.json' if proposal.candidate else ''}).")
    if proposal.verdict == "safe_update" and auto_apply:
        backup = apply_proposal(out, connector_path)
        typer.echo(f"Applied. Previous version saved as {backup}.")
        return
    if proposal.has_update:
        typer.echo(f"Review it, then: connectorgen apply {out} {connector_path}")
    raise typer.Exit(EXIT_UPDATE if proposal.verdict == "safe_update" else EXIT_REVIEW)


@app.command("apply")
def apply_cmd(
    proposal_dir: Path = typer.Argument(...),
    connector_path: Path = typer.Argument(...),
    yes: bool = typer.Option(False, "-y", "--yes", help="Don't ask for confirmation."),
    force: bool = typer.Option(False, help="Apply even if the connector file changed since the check."),
) -> None:
    """Apply an update proposal from `check` (keeps a timestamped backup)."""
    from .repair import RepairProposal, apply_proposal

    try:
        proposal = RepairProposal.load(proposal_dir)
    except (OSError, ValueError) as e:
        typer.secho(f"Error: can't read proposal: {e}", fg="red", err=True)
        raise typer.Exit(1)
    typer.echo(proposal.to_text())
    if not yes and not typer.confirm(f"\nApply this to {connector_path}?"):
        raise typer.Exit(1)
    try:
        backup = apply_proposal(proposal_dir, connector_path, force=force)
    except ConnectorError as e:
        typer.secho(f"Error: {e}", fg="red", err=True)
        raise typer.Exit(1)
    typer.echo(f"Applied. Previous version saved as {backup}.")


@app.command("diff")
def diff_cmd(
    old: Path = typer.Argument(...),
    new: Path = typer.Argument(...),
    used: list[str] = typer.Option([], "--used"),
) -> None:
    """Compare two connector files and classify every change by impact."""
    from .repair import diff_connectors

    d = diff_connectors(Connector.load(old), Connector.load(new))
    typer.echo(d.to_text(set(used)))
    if d.breaking:
        raise typer.Exit(EXIT_REVIEW)


def _make_llm(model: str | None, no_cache: bool, base_url: str | None = None):
    from .llm import CachedLLM, llm_from_spec

    llm = llm_from_spec(model, base_url=base_url)
    return llm if no_cache else CachedLLM(llm)



@app.command()
def enrich(
    connector_path: Path = typer.Argument(..., help="Generated connector.json"),
    output: Optional[Path] = typer.Option(None, "-o", "--output",
                                          help="Where to write (default: overwrite input)."),
    passes: str = typer.Option("pagination,auth,describe",
                               help="Comma-separated: pagination, auth, describe."),
    docs: list[str] = typer.Option([], "--docs", help="Doc URL/file to help the auth pass."),
    rename_all: bool = typer.Option(False, help="Also rename actions the spec named "
                                                "(breaks existing users of those names)."),
    model: Optional[str] = typer.Option(None, help=_MODEL_HELP),
    llm_base_url: Optional[str] = typer.Option(None, envvar="CONNECTORGEN_LLM_BASE_URL", help=_LLM_URL_HELP),
    no_cache: bool = typer.Option(False, help="Bypass the on-disk response cache."),
    dry_run: bool = typer.Option(False, help="Show proposals without writing."),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Fill gaps with an LLM: pagination, auth, names and descriptions. Every behavioral
    change must pass the contract tests to be kept. Needs the chosen provider's API key."""
    from .enrich import enrich as run_enrich
    from .ingest.docs import fetch_docs

    c = Connector.load(connector_path)
    docs_text = "\n\n".join(p.text for p in fetch_docs(docs)) if docs else None
    try:
        enriched, report = run_enrich(c, _make_llm(model, no_cache, llm_base_url),
                                      passes=[p.strip() for p in passes.split(",") if p.strip()],
                                      docs_text=docs_text, rename_all=rename_all)
    except (ConnectorError, ValueError) as e:
        typer.secho(f"Error: {e}", fg="red", err=True)
        raise typer.Exit(1)
    typer.echo(report.to_text(verbose=verbose))
    if dry_run:
        typer.secho("Dry run: nothing written.", fg="yellow")
        return
    target = output or connector_path
    enriched.save(target)
    typer.echo(f"Wrote {target}")


@app.command("from-docs")
def from_docs(
    sources: list[str] = typer.Argument(..., help="Doc page URLs or local HTML/Markdown files."),
    output: Path = typer.Option(Path("connector.json"), "-o", "--output"),
    name: Optional[str] = typer.Option(None),
    base_url: Optional[str] = typer.Option(None, help="API base URL (overrides the docs)."),
    crawl: bool = typer.Option(False, help="Follow links under the same docs directory."),
    max_pages: int = typer.Option(15),
    model: Optional[str] = typer.Option(None, help=_MODEL_HELP),
    llm_base_url: Optional[str] = typer.Option(None, envvar="CONNECTORGEN_LLM_BASE_URL", help=_LLM_URL_HELP),
    no_cache: bool = typer.Option(False),
    skip_enrich: bool = typer.Option(False, help="Skip the follow-up enrichment passes."),
) -> None:
    """Build a connector from API docs when there's no OpenAPI spec. Needs an LLM API key."""
    from .enrich import enrich as run_enrich
    from .ingest.docs import connector_from_docs
    from .validate import run_contract_tests

    try:
        llm = _make_llm(model, no_cache, llm_base_url)
        connector, report = connector_from_docs(sources, llm, name=name, base_url=base_url,
                                                crawl=crawl, max_pages=max_pages)
    except ConnectorError as e:
        typer.secho(f"Error: {e}", fg="red", err=True)
        raise typer.Exit(1)
    typer.echo(report.to_text())
    if not skip_enrich and connector.actions:
        connector, enrich_report = run_enrich(connector, llm, passes=["pagination", "auth"])
        if enrich_report.changes or enrich_report.errors:
            typer.echo(enrich_report.to_text())
    connector.save(output)
    contract = run_contract_tests(connector)
    typer.echo(f"Wrote {output}: {len(connector.actions)} actions; contract tests: "
               f"{contract.passed} passed, {contract.failed} failed, {contract.skipped} skipped")
    if contract.failed:
        typer.secho("Run `connectorgen test` to see failures; review before use.", fg="yellow")


@app.command()
def call(
    connector_path: Path = typer.Argument(...),
    action: str = typer.Argument(...),
    inputs: list[str] = typer.Option([], "-i", "--input",
                                     help="key=value (value parsed as JSON when possible)"),
    body: Optional[Path] = typer.Option(None, help="JSON file to send as the request body."),
    paginate: bool = typer.Option(False, help="Follow pagination and print all items."),
    max_pages: Optional[int] = typer.Option(None),
    base_url: Optional[str] = typer.Option(None),
    allow_destructive: bool = typer.Option(False, help="Required for destructive actions."),
    token_file: Optional[Path] = typer.Option(None, help="Saved OAuth token (from `auth`)."),
) -> None:
    """Call an action. Credentials come from CONNECTORGEN_API_KEY / _TOKEN / _USERNAME / _PASSWORD."""
    c = Connector.load(connector_path)
    act = c.action(action)
    if act.destructive and not allow_destructive:
        typer.secho(f"'{action}' is destructive; pass --allow-destructive.", fg="red", err=True)
        raise typer.Exit(1)
    params: dict = {}
    for item in inputs:
        key, sep, raw = item.partition("=")
        if not sep:
            raise typer.BadParameter(f"Expected key=value, got '{item}'")
        try:
            params[key] = json.loads(raw)
        except json.JSONDecodeError:
            params[key] = raw
    if body:
        try:
            params["body"] = json.loads(body.read_text())
        except (OSError, json.JSONDecodeError) as e:
            typer.secho(f"Error: couldn't read JSON body from {body}: {e}", fg="red", err=True)
            raise typer.Exit(1)

    creds = _credentials(c, token_file)
    try:
        if c.mcp is not None:
            from .mcp import McpClient

            if paginate:
                raise typer.BadParameter("--paginate doesn't apply to MCP tools.")
            with McpClient(c, creds) as client:
                result = client.execute(act, params)
        else:
            with ConnectorClient(c, creds, base_url=base_url) as client:
                if paginate:
                    result = list(client.paginate(act, params, max_pages=max_pages))
                else:
                    result = client.execute(act, params)
    except ConnectorError as e:
        typer.secho(f"Error: {e}", fg="red", err=True)
        raise typer.Exit(1)
    typer.echo(json.dumps(result, indent=2) if not isinstance(result, str) else result)


if __name__ == "__main__":
    app()
