# OAuth2

OAuth flows (`authorizationCode`, `clientCredentials`) are read from the spec.

```bash
# Register http://127.0.0.1:8765/callback as a redirect URI with the provider first.
export CONNECTORGEN_CLIENT_ID=... CONNECTORGEN_CLIENT_SECRET=...
connectorgen auth acme.json                    # opens the browser once, saves the token
connectorgen call acme.json get_me --token-file .connectorgen_tokens/acme.json
```

- Authorization code uses **PKCE (S256)** and checks `state` to block forged redirects.
- Tokens refresh automatically 60 s before expiry, and once more if the API returns 401.
  Client-credentials tokens are simply re-fetched.
- Token files are written atomically with **owner-only (0600)** permissions, and tokens never
  appear in logs or `repr()`.

```python
from connectorgen.runtime.oauth import OAuth2Session, FileTokenStore, run_loopback_flow

session = OAuth2Session(c.auth[0].oauth_flows[0], client_id, client_secret,
                        store=FileTokenStore(".connectorgen_tokens/acme.json"))
run_loopback_flow(session)                     # once
client = ConnectorClient(c, Credentials(oauth=session))
```

[← Back to the README](../README.md)
