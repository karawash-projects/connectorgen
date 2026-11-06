"""Which actions count as destructive.

`destructive` gates `call` (needs --allow-destructive), `serve` (left out unless allowed)
and smoke tests (never called), so it errs on the side of caution: PUT, PATCH and DELETE
replace or remove data, and a POST is destructive when its name, path or summary says so.
"""

from __future__ import annotations

import re

from ..ir import Action

# Older, name-only rule; still applied as a fallback.
DESTRUCTIVE_WORDS = re.compile(
    r"(^|_)(delete|remove|destroy|purge|cancel|revoke|erase|wipe|truncate|terminate)(_|$)")

# Verbs that destroy, disable, revoke, move away or pay out. Matched as whole words in the
# action's name and path (split on camelCase and punctuation) and as the leading verb of its
# summary, so "dropdown", "dropbox" or "Deleted items are excluded" don't count.
DESTRUCTIVE_VERBS = {
    "delete", "remove", "destroy", "purge", "erase", "wipe", "truncate", "terminate", "flush",
    "reset", "deactivate", "disable", "close", "drop", "kill", "clear", "uninstall", "suspend",
    "revoke", "cancel", "void", "ban", "expire", "overwrite", "unpublish", "unsubscribe",
    "detach", "unlink", "prune", "stop", "kick", "invalidate", "disconnect", "rotate",
    "regenerate", "transfer", "rollback", "anonymize", "anonymise", "empty", "abort", "discard",
    "deprovision", "refund", "evict", "replace", "trash", "undeploy", "logout", "signout",
    "retire", "offboard", "demote", "capture", "payout", "merge", "force", "commit", "password", "block", "deny", "decommission", "archive",
    "lock", "freeze", "shutdown", "restart", "reboot", "reinstall", "downgrade", "leave",
    "unassign", "uninvite", "unshare", "unfollow", "unenroll", "dismiss", "withdraw",
    # generic executors: whatever they run (SQL, a mutation, any method) could destroy data
    "execute", "exec", "eval", "sql", "rpc", "graphql", "mutation", "mutate", "command",
    "shell", "script",
}
# Anywhere in a summary or description, these mean "runs arbitrary statements".
_EXECUTOR_WORDS = {"sql", "mutation", "mutations", "shell"}
_IRREVERSIBLE = re.compile(r"\bpermanent(ly)?\b|irreversib|can(not|'t| not) be (undone|recovered|reversed)",
                           re.IGNORECASE)
# An endpoint ending in one of these reads or checks something, even when an earlier path
# segment names a destructive thing (/hr/leave/balance, /sql/validate, /locks/status).
READ_ONLY_WORDS = {"status", "list", "search", "find", "get", "validate", "validation", "check",
                   "preview", "estimate", "count", "analyze", "analyse", "lookup", "eligibility",
                   "balance", "history", "stats", "info", "describe", "verify", "simulate",
                   "dry", "render", "quote", "report", "export", "download", "policy", "policies",
                   "analyze", "query", "extract", "screen", "read", "fetch", "compute", "calculate"}
_WORD = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])")


def _verb_forms(word: str) -> set[str]:
    w = word.lower()
    forms = {w}
    for suffix in ("es", "s"):
        if w.endswith(suffix) and len(w) > len(suffix) + 2:
            forms.add(w[: -len(suffix)])
    return forms


def action_destructive(act: Action) -> bool:
    """Docs-made actions are named by a model, so be conservative: PUT/PATCH/DELETE replace
    or remove data, and a POST is destructive when its name or path contains a destructive
    verb, or its summary starts with one."""
    if act.method in ("PUT", "PATCH", "DELETE"):
        return True
    if act.method == "GET":
        return False
    summary_words = _WORD.findall(act.summary or "")
    if summary_words and _verb_forms(summary_words[0]) & DESTRUCTIVE_VERBS:
        return True
    text = f"{act.summary} {act.description}"
    if _IRREVERSIBLE.search(text):
        return True
    if summary_words and _verb_forms(summary_words[0]) & READ_ONLY_WORDS:
        return False   # "Get the cancellation policy", "Analyze stop words"
    described = {w.lower() for w in _WORD.findall(text)}
    if described & _EXECUTOR_WORDS:
        return True    # "Runs a SQL statement": whatever it runs could destroy data
    literal = [seg for seg in act.path.strip("/").split("/") if seg and not seg.startswith("{")]
    last_words = {w.lower() for w in _WORD.findall(literal[-1])} if literal else set()
    name_words = [w.lower() for w in _WORD.findall(act.name)]
    if last_words & READ_ONLY_WORDS or (name_words and name_words[0] in READ_ONLY_WORDS):
        return False
    words = _WORD.findall(act.name) + _WORD.findall(act.path)
    return any(_verb_forms(w) & DESTRUCTIVE_VERBS for w in words) or \
        bool(DESTRUCTIVE_WORDS.search(_snake(act.name)))


docs_action_destructive = action_destructive  # the name docs.py used in 0.9.0


def _snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()
