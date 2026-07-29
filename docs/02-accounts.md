[← Index](./index.md)

# 2 · Accounts

`Account` is a **handle to the account at an address** in an SVM — the lens through which you read that account's state and, when it holds a keypair, act as fee payer / signer. Two things the name elides:

- **It need not exist on-chain.** Every address has an account *slot*; "doesn't exist" just means empty / not-yet-created. Check `exists`; `lamports` reads 0 for such a slot, while `data` / `owner` / `executable` / `rent_epoch` raise until the account is funded.
- **It may or may not hold a private key.** It can sign only when created with a keypair (`Account.new()` / `Account.from_secret()`); a bare-address view or a PDA is read/inspect-only (`can_sign == False`).

## Creating / viewing / deriving

```python
from wake_sol import Account, Pubkey, svm

a = Account.new()                       # fresh keypair → can sign
b = Account.from_secret(secret_bytes)   # known 64-byte secret → can sign
v = Account(some_pubkey)                # bare-address view → cannot sign
pda, bump = Account.find_program_address([b"seed", a.pubkey.to_bytes()], program_id)
# the same address again, with the bump spelled out (no search)
pda2 = Account.create_program_address([b"seed", a.pubkey.to_bytes(), bytes([bump])], program_id)
```

All of these bind to the global `svm` unless you pass `svm=other` (see [§5](05-svm-and-sysvars.md)). Keypairs created via `Account.new()`/`from_secret()` are remembered process-wide, so the harness can sign for them automatically later (see [§3](03-transactions.md)).

When you only need the **address** (no SVM-bound handle), the same derivations exist on `Pubkey` and return a bare `Pubkey`:

```python
pda, bump = Pubkey.find_program_address([b"seed"], program_id)   # -> (Pubkey, int)
pda2 = Pubkey.create_program_address([b"seed", bytes([bump])], program_id)  # -> Pubkey
```

## Reading state

Every read delegates to the bound SVM, so a handle is always current:

```python
acc.pubkey        # Pubkey (the address)
acc.exists        # bool — is there an account at this address?
acc.lamports      # int  — balance (0 if it doesn't exist)
acc.data          # bytes — account data (raises if it doesn't exist)
acc.can_sign      # bool — does it hold a keypair?
acc.secret        # bytes — 64-byte secret (raises if no keypair)
acc.svm           # the LiteSVM this handle is bound to
```

`acc.sign(message)` produces a detached ed25519 signature over arbitrary bytes as a `SignedMessage` (raises if the account can't sign) — for the signature-verification precompiles and off-chain signature checks; see [§9](09-signing-and-precompiles.md).

> `lamports` is typed `u64`, so it flows straight into builder args / struct fields of that width without a type-checker complaint. See [§6](06-types-and-encoding.md).

## Funding & seeding accounts

Three ways to put state at an address:

```python
# 1. airdrop lamports (creates/credits the account)
svm.airdrop(acc, 1_000_000_000)

# 2. overwrite the whole account (lamports + data + owner + flags)
svm.set_account(addr, lamports=2_000_000, data=b"...", owner=some_program,
                executable=False, rent_epoch=0)

# 3. write the balance alone, leaving data / owner / flags untouched
acc.lamports = 2_000_000
acc.lamports += 500        # top up
acc.lamports -= 500        # burn
```

All three are **cheatcodes** — they mint and burn lamports out of thin air, which no real chain permits. `acc.lamports = …` is the quietest of them: no transaction, no fee, no `TransactionResult`, just a state write. Two edges to know:

- **Assigning 0 deletes the account** — data and owner go with it. That is faithful to Solana, where a zero-lamport account is reaped, but it makes `acc.lamports = 0` a rather bigger hammer than it looks. To drain an account and keep its data, leave it a lamport.
- **A balance can't go negative.** `acc.lamports -= too_much` raises `ValueError` (Python does the subtraction, so the setter sees the negative result and reports it against the current balance).

Writing the balance of a *program* account re-loads its ELF into the program cache, and writing a *sysvar* account re-parses its data into the sysvar cache — correct, but not free.

`set_account` is how you seed arbitrary program state for a test — e.g. write a Borsh-encoded account body (with its 8-byte discriminator) produced by `SomeAccount(...).encode()` (see [§6](06-types-and-encoding.md)):

```python
data = MyAccount(owner=alice.pubkey, amount=42).encode()   # discriminator + body
svm.set_account(pda, lamports=svm.minimum_balance_for_rent_exemption(len(data)),
                data=data, owner=PROGRAM_ID)
```

Make an account rent-exempt with `svm.minimum_balance_for_rent_exemption(space)` (see [§5](05-svm-and-sysvars.md)).

## Labels

Assign a human name to an address; it shows up in call-trace rendering and `str(account)`:

```python
alice.label = "alice"
str(alice)            # "alice"  (else a truncated base58, or a well-known program name)
```

## Equality & hashing

Two `Account`s are equal when they share the same address (keypair ignored), and they hash by address — so they work as dict keys / set members.
