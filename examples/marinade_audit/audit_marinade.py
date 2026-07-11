#!/usr/bin/env python
"""End-to-end audit-workflow example (handover 02) against **Marinade Finance**.

Demonstrates the whole composition on a real mainnet program:

    fork mainnet  ->  pin the program (offline-replayable)  ->  gen pytypes
    ->  decode real on-chain bytes  ->  build + drive an instruction  ->  decode the trace

This is a **live, manual** example — it talks to mainnet and is deliberately NOT
part of the hermetic test suite. Run it with a mainnet RPC endpoint:

    SOLANA_RPC_URL=<https-rpc>  .venv/bin/python examples/marinade_audit/audit_marinade.py

Target: Marinade `MarBmsSgKXdrN1egZf5sqe1TMai9K1rChYNDJgjq7aD` (upgradeable,
~2 MB programdata, 28 instructions). The Anchor IDL is captured under `idls/`
straight from the program's on-chain IDL account, so the example is
self-contained. (Caveat: an on-chain IDL can lag deployed bytecode — fine here.)

Turning this into a real audit of *your* build is a one-line swap at Stage 0:
    svm.fork(url, exclude=[MARINADE]); svm.add_program(MARINADE, my_patched_elf)
— fork blanks the audited program's code so you deploy your own, while its real
mainnet dependencies (state, pools, mints) fork in around it untouched.
"""

import base64
import hashlib
import importlib
import json
import os
import struct
import sys
import tempfile
import urllib.request
from pathlib import Path

from solana_fuzzer import Account, LiteSVM, Pubkey, decode_instruction
from solana_fuzzer._gen import run_gen

MARINADE = "MarBmsSgKXdrN1egZf5sqe1TMai9K1rChYNDJgjq7aD"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SYSTEM = "11111111111111111111111111111111"
HERE = Path(__file__).parent
IDL_DIR = HERE / "idls"
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


# --- tiny JSON-RPC + base58 (no extra deps) -------------------------------- #

def _rpc(url, method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    req = urllib.request.Request(url, data=body.encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=40) as resp:
        out = json.load(resp)
    if "error" in out:
        raise RuntimeError(f"rpc {method}: {out['error']}")
    return out["result"]


def _b58decode(s):
    num = 0
    for ch in s:
        num = num * 58 + _B58.index(ch)
    body = num.to_bytes((num.bit_length() + 7) // 8, "big")
    return b"\x00" * (len(s) - len(s.lstrip("1"))) + body


# --- stages ---------------------------------------------------------------- #

def gen_pytypes():
    """Generate the marinade pytypes package from the captured IDL and import it
    (import triggers self-registration with the decode engine)."""
    out_root = Path(tempfile.mkdtemp(prefix="marinade-pytypes-"))
    rc = run_gen(target_idls=(str(IDL_DIR),), dep_idls=("/nonexistent",),
                 out=str(out_root / "pytypes"))
    assert rc == 0, "gen failed"
    sys.path.insert(0, str(out_root))
    for name in [n for n in sys.modules if n == "pytypes" or n.startswith("pytypes.")]:
        del sys.modules[name]
    return importlib.import_module("pytypes").marinade_finance


def decode_real_instruction(url):
    """Reality check: pull a recent marinade tx and decode its instruction from
    the raw on-chain bytes via the generated pytypes. Returns the account keys of
    a `deposit` we can template Stage 5 from (or None)."""
    sigs = _rpc(url, "getSignaturesForAddress", [MARINADE, {"limit": 25}])
    deposit_template = None
    for s in sigs:
        tx = _rpc(url, "getTransaction",
                  [s["signature"], {"maxSupportedTransactionVersion": 0, "encoding": "json"}])
        if not tx:
            continue
        msg = tx["transaction"]["message"]
        keys = msg["accountKeys"]
        for ix in msg["instructions"]:
            if keys[ix["programIdIndex"]] != MARINADE:
                continue
            data = _b58decode(ix["data"])
            accts = [keys[i] for i in ix["accounts"]]
            dec = decode_instruction(Pubkey(MARINADE), data, len(accts))
            print(f"   decoded real {s['signature'][:12]}…  "
                  f"name={dec.name!r}  args={getattr(dec, 'args', dec)}")
            if dec.name == "deposit" and len(accts) == 11 and deposit_template is None:
                deposit_template = accts
        if deposit_template:
            return deposit_template
    return deposit_template


def build_roundtrip(mfin):
    """Build a deposit with the generated builder, then decode our own bytes back
    — proves the encode leg agrees with the decode leg."""
    ix = mfin.MarinadeFinance().deposit(
        1_000_000,
        state=Pubkey(1), msolMint=Pubkey(2), liqPoolSolLegPda=Pubkey(3),
        liqPoolMsolLeg=Pubkey(4), liqPoolMsolLegAuthority=Pubkey(5), reservePda=Pubkey(6),
        transferFrom=Pubkey(7), mintTo=Pubkey(8), msolMintAuthority=Pubkey(9),
    )
    dec = decode_instruction(Pubkey(MARINADE), ix.data, len(ix.accounts))
    assert dec.name == "deposit" and dec.args["lamports"] == 1_000_000, dec
    print(f"   built deposit -> {len(ix.data)}B data, {len(ix.accounts)} accounts; "
          f"round-trip decode: {dec.name} {dec.args}")


def _spl_token_account(mint_b58, owner_b58):
    """A minimal initialized SPL token account (165 bytes, amount 0)."""
    buf = bytearray(165)
    buf[0:32] = Pubkey(mint_b58).to_bytes()
    buf[32:64] = Pubkey(owner_b58).to_bytes()
    struct.pack_into("<Q", buf, 64, 0)   # amount
    buf[108] = 1                          # AccountState::Initialized
    return bytes(buf)


def drive_deposit(url, svm, mfin, template):
    """Best-effort: execute a real deposit under the fork. We reuse the marinade-
    side accounts from a real tx (they hydrate on touch) and substitute our own
    funded payer + mSOL token account. Whatever happens, we decode the trace."""
    if not template:
        print("   (no deposit template found on chain; skipping)")
        return
    state, msol_mint, liq_sol, liq_msol, liq_msol_auth, reserve, _from, _to, msol_auth = template[:9]

    payer = Account.new(svm)                                  # fresh signer
    svm.set_account(payer.pubkey, lamports=5_000_000_000, owner=SYSTEM)
    msol_ata = Account.new(svm)
    svm.set_account(msol_ata.pubkey, lamports=2_100_000, owner=TOKEN_PROGRAM,
                    data=_spl_token_account(msol_mint, str(payer.pubkey)))

    ix = mfin.MarinadeFinance().deposit(
        1_000_000_000,  # 1 SOL
        state=state, msolMint=msol_mint, liqPoolSolLegPda=liq_sol, liqPoolMsolLeg=liq_msol,
        liqPoolMsolLegAuthority=liq_msol_auth, reservePda=reserve,
        transferFrom=payer.pubkey, mintTo=msol_ata.pubkey, msolMintAuthority=msol_auth,
    )
    res = payer.tx(ix)
    print(f"   deposit executed: success={res.success} "
          f"cu={res.compute_units_consumed} error={res.error}")
    for line in (res.logs or [])[:6]:
        print(f"     log: {line}")
    # Decode the top-level instruction straight off the trace.
    trace = res.call_trace
    if trace and len(trace):
        top = trace[0]
        dec = decode_instruction(top.program_id, top.data, len(top.accounts))
        print(f"   trace[0] decoded: {dec.name} {getattr(dec, 'args', '')}")


def main():
    url = os.environ.get("SOLANA_RPC_URL")
    if not url:
        sys.exit("set SOLANA_RPC_URL to a mainnet RPC endpoint")

    print("Stage 0 · fork mainnet")
    svm = LiteSVM()
    svm.fork(url, cache=str(HERE / ".fork-cache"))

    print("Stage 1 · pin the program (fork_programs) + introspect")
    n = svm.fork_programs(MARINADE)
    print(f"   pinned {n} program(s); forked_accounts():")
    for a in svm.forked_accounts():
        kind = "program" if a.executable else "programdata"
        print(f"     {a.pubkey}  {kind:11} {len(a.data):>9,}B")

    print("Stage 2 · gen pytypes from the captured IDL + import")
    mfin = gen_pytypes()
    print(f"   imported {mfin.__name__} (PROGRAM_ID={mfin.PROGRAM_ID})")

    print("Stage 3 · decode a REAL on-chain instruction via the generated pytypes")
    template = decode_real_instruction(url)

    print("Stage 4 · build with the generated builder + round-trip decode")
    build_roundtrip(mfin)

    print("Stage 5 · drive a live deposit under the fork + decode its trace")
    drive_deposit(url, svm, mfin, template)

    print("\nDone — fork + pin + gen + decode composed against live mainnet.")


if __name__ == "__main__":
    main()
