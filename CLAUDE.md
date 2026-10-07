# trixie-gateway — notes for AI agents

aiohttp gateway (PC side of the TrXi-Ctrl phone app). Runs as systemd
`trixie-gateway.service` on :8772 from this directory in `/root/pyqt6-venv`
(`systemctl restart trixie-gateway` to load changes — drops phone sessions).
`../trixie-gateway-pro` is the licensed variant: mirror every change there too.

## Adding a feature
1. Logic in `capabilities/<name>.py` (async functions; run blocking work with
   `asyncio.to_thread`). Import it in the `from capabilities import ...` line.
2. Routes in `_build_app()` (`app.router.add_get/post/put/delete("/yay/<name>/...")`)
   and `async def _<name>_*` handlers returning `web.json_response(...)`.
3. Journal writes: `self.journal.append("<type>", detail)` — the type MUST be
   in `semantic_journal.KNOWN_TYPES` or the handler 500s (AssertionError).
4. Auth: every non-public route needs a token with scope `device`/`admin`;
   paired phones hold `admin`. Public routes: `_PUBLIC_ROUTES`. Non-Linux
   features: `_FEATURE_ROUTES` + `platforms.FEATURES`.

## Testing without touching the live gateway
Second instance with its own HOME/config (auth off):
```
HOME=$SP/home nohup /root/pyqt6-venv/bin/python gateway.py --port 8799 \
  --socket $SP/gw.sock --config $SP/config.json &   # config: {"capabilities": {"allow_unauthenticated": true}} (only works from this PC)
```
Stop it by PID, not `pkill -f "gateway.py --port 8799"` from the same shell —
that pattern matches the shell's own command line and kills it.

MCP servers here (`media_mcp_server.py`) are thin JSON-RPC wrappers over the
same `capabilities/` module the REST routes use — follow that pattern.

Desktop control panel: `gateway_manager.py` (PySide6, launcher
`~/.local/share/applications/custom-gateway-manager.desktop`), which does
start/stop/restart of the free or Pro unit, shows the pairing QR, and tails the journal.

Security regression tests (cross-site/DNS-rebinding refusal, auth on by
default): `/root/pyside6-venv/bin/python3 test_security.py` (starts its own
throwaway gateways; run after touching auth_middleware). Same file in -pro.
