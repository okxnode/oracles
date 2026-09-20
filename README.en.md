# oracles

**Manage all your Oracle Cloud (OCI) accounts from a single Telegram bot.**

Everything is built on the **official Oracle Python SDK** (the `oci` package) — no `oci` CLI dependency, no shelling out, no parsing of command-line output.

**Language / 语言：** [English](README.en.md) · [中文](README.md)

```
                    ┌──────────────────────────────┐
   Telegram  ──────▶│   oracles-bot (systemd)      │
   (buttons + cmds) │                              │
                    │  bot/      interaction layer │
                    │            + confirmations   │
                    │  services/ business logic    │
                    │  oci_gateway  official SDK   │
                    └──────────────┬───────────────┘
                                   │  oci Python SDK
                    ┌──────────────▼───────────────┐
                    │  acct 1  │ acct 2 │ … │acct N│
                    │  Compute · Block Volume ·    │
                    │  Object Storage · Network ·  │
                    │  Limits · Identity           │
                    └──────────────────────────────┘
```

---

## What it does

`/start` gives you **seven main entry points** (button-driven; the commands are just shortcuts):

| # | Capability | Details |
| --- | --- | --- |
| 1 | ⚙️ **Config management** | Add/remove/inspect API credentials: edit the account list (**auto-snapshots to `backups/` before any edit or delete**, so a mistake is recoverable), per-account credential health check (key file / OCID / fingerprint, with an optional *live* check that makes a real API call), hot reload after adding — no restart |
| 2 | 🚀 **Launch / capacity hunting** | Step-by-step wizard: shape (E2 Micro free tier / A1.Flex with custom OCPU+memory) → OS image → boot volume size → instance count → login method (**SSH public key** — either *use the one you configured* or *let the bot generate a pair and store it on the server, re-downloadable anytime* — or **username + password injected via cloud-init**) → IPv6. Then one of two modes: **"Launch now"** (try each instance once; if one fails, wrap up and report) or **"Auto-hunt 5/10/20/30 s"** (background retry loop until N instances are up or you stop it; each interval gets its own row so the number is fully visible). Both try to reach the count you picked — the only difference is **what happens after a failure** |
| 3 | 🖥 **Instance management** | List (with public IP / state), start, stop, reboot, terminate, 🔑 **download login key** (reads the public key actually injected into the instance metadata and **verifies the fingerprint first** — on mismatch it says plainly "this key can't open that machine"), and 🚑 **rescue** — opens a serial console plus a VNC tunnel so you can watch the screen and type into a shell when SSH is dead (free accounts allow one console per instance; clicking again replaces the old one) |
| 4 | 📊 **Quota** | Whole-fleet overview or per-account detail: E2/A1 headroom, block-storage quota, "how many more instances fit" (summed **per availability domain** — see "The biggest trap" below) |
| 5 | 💾 **Volume management** | Boot + block volume list (with attachment state: 🟢 attached / ⚪ free / ❓ unknown), **resize**, delete unattached volumes. The delete button **only appears when the volume is confirmed unattached** — attached volumes and volumes whose attachment state couldn't be read get no entry point at all. After you confirm, the attachment relation is **re-checked live**; attached *or* unverifiable both get refused. Orphan volumes are the block-storage quota killer — cleaning them up frees quota immediately |
| 6 | 🧵 **Task management** | Progress board for auto-hunt tasks: x/N instances, attempt count, last failure reason; one-tap stop |
| 7 | 🪣 **Bucket management** | List, create, delete buckets (clears blocking PARs first), browse objects, issue S3-compatible keys |

Plus two cross-cutting tools: 🧹 **billing-residue audit** (unattached public IPs / orphan volumes / image backups)
and 🛡 **security exposure scan** (rules opening port 22 to the world, plus plaintext passwords in `user_data` — with one-tap tightening of the source CIDR).

---

## Security design (the top priority for an open-source project)

This bot holds the private keys to **all** of your OCI accounts, so security isn't a "nice to have" — it's part of the architecture.

### 1. Keys never live in the repository

```
repository (pushed to GitHub)     config dir (never in the repo)
─────────────────────────         ──────────────────────────
accounts.example.json     ──▶     /etc/oracles/accounts.json   (600)
.env.example              ──▶     /etc/oracles/oracles.env     (600)
                                  /etc/oracles/keys/*.pem       (600)
```

- The account list and private keys are read from `$ORACLES_HOME` (default `~/.oracles`, `/etc/oracles` in production) — **outside the repository tree**
- `.gitignore` covers `accounts.json` / `*.pem` / `.env` / `backups/`
- `scripts/check_secrets.sh` scans before you commit; installable as a git pre-commit hook
- `.github/workflows/secret-scan.yml` adds a second gate in CI

### 2. Two independent write switches

```bash
ORACLES_WRITE_ENABLED=false      # off → every write operation becomes a DRY-RUN
ORACLES_ALLOW_DESTRUCTIVE=false  # off → destroy/delete operations get a separate gate
```

Both are **off by default**. Changing them requires SSH access to the host, so even if your Telegram account is compromised, the attacker can only look — not delete.

### 3. Destructive operations go "plan first → human confirms"

Every write operation is two-phase:

```
user taps "Launch instance"
      ↓
plan_launch()  ← read-only: pick AD, check quota, find image, verify block storage
      ↓
show the full plan (name/shape/AD/image/subnet/boot volume/SSH key + fingerprint)
      ↓
user taps "✅ Confirm"
      ↓
execute_launch()  ← only now is the API actually called
```

This is two separate functions in the code — DRY-RUN isn't an `if` inside a write path,
it's **structurally impossible to execute by accident**.

### 4. Logs and messages are fully redacted

`oracles/redact.py` replaces OCIDs, fingerprints, PEM private keys,
Telegram tokens and S3 keys with placeholders before anything leaves the process.
Suspected password fragments shown in audit reports are masked too.

### 5. Allow-list denies by default

When `TELEGRAM_ALLOWED_USER_IDS` is empty the bot **responds to nobody** — not "open to everyone because it wasn't configured".
A bot that can delete your cloud instances, sitting exposed on the internet, is a disaster the moment someone finds it.

---

## The biggest trap: free-tier quota is bound to a **single availability domain**

This isn't a quirk of OCI — it's the root cause that makes every "grab the first AD and launch" implementation fail forever.

| Account | Region | AD-1 | AD-2 | AD-3 |
| --- | --- | --- | --- | --- |
| Account 13 | us-ashburn-1 | 0 | 0 | **2** |
| Account 17 | eu-frankfurt-1 | 0 | **2** | 0 |
| Account 9 | us-ashburn-1 | **2 (used up)** | 0 | 0 |

Within one region, **account 9 and account 13 have completely independent quotas**; within one account,
it's normal for AD-1 to have 2 cores while AD-2 has 0.

**Consequence**: any script that "takes the first AD and launches" will always pick AD-1 for accounts 13/17,
hit zero quota there, and have the error misread as `Out of host capacity`.

What this project does instead:

```python
cap = account_capacity(client)              # query standard-e2-micro-core-count per AD
ad  = pick_availability_domain(cap, shape=shape, cores=ocpus)   # pick the AD with most headroom
plan = plan_launch(client, ad=ad)           # then create
```

Capacity formula (the smaller of two constraints):

```
instances an AD can hold = min(CPU headroom in that AD, floor(block-storage headroom / 47))
account total            = Σ over each AD              ← must be summed per AD
```

> **E2 and A1 are two separate limits** (`standard-e2-micro-core-count` /
> `standard-a1-core-count`), so "which AD has room" is **bound to the shape**.
> On one account we measured: E2 was 2/2 used up while **A1 still had 2 cores** —
> picking an AD using E2's accounting returns "no AD has headroom",
> making the A1 option permanently unable to launch. So AD selection must carry
> both the shape and the cores per instance.
> (A1 also needs **division by cores per instance**, otherwise "2 cores left" is
> treated as "can fit 2 instances with 4 cores each".)

> One related trap: `bootVolumeQuota Service limit reached` reports **block storage**
> exhaustion, not CPU. A free-tier tenancy gets only 200 GB of block storage
> (boot volumes + block volumes combined); once the pool is full you can't launch
> instances **even with CPU quota to spare**. The usual culprit is orphan boot volumes —
> a single leftover 150 GB volume eats 75% of the quota. Check with `/audit`.

---

## Getting started

### Prerequisite: get API credentials from the OCI console

Each account needs four values (Console → avatar top-right → **My profile** → **API keys** → **Add API key**):

| Field | Where it comes from |
| --- | --- |
| `user` | user OCID |
| `fingerprint` | shown after you add the API key |
| `tenancy` | tenancy OCID |
| `region` | e.g. `ap-singapore-1` |

The downloaded private key (`*.pem`) **can be shared by all accounts**.

### One-command install

Prerequisites: Ubuntu 22.04+ / Debian 12+, Python 3.10+, root, and network access to pypi and api.telegram.org.
**Don't forget `python3-venv`** (Ubuntu doesn't ship it by default, and the package name carries a version):

```bash
sudo apt-get update
sudo apt-get install -y python3-venv python3.14-venv   # swap in your interpreter's version
```

```bash
git clone https://github.com/okxnode/oracles.git
cd oracles
sudo ./deploy/install.sh
```

`install.sh` does six things and is **idempotent** (re-running only updates code and dependencies, never overwrites existing config):

| Step | What it does | Why |
| --- | --- | --- |
| 0 | **Scan the source tree for secrets first** | The next step rsyncs the whole source tree to `/opt/oracles`. If a `si.pem` were lying around, that copy would plant it into a system directory — so we stop **before** copying |
| 0.5 | **Check whether `python3` can create venvs** | Ubuntu ships without `ensurepip`, and the error never tells you "the package name has a version suffix" or "run `apt-get update` first". Better to stop now than to blow up at step 2 (by which point the user and directories already exist) |
| 1 | Create the `oracles` system user + `/opt/oracles` | The bot must not run as root |
| 2 | Create a venv, install dependencies | Isolation — never touches the system Python |
| 3 | Create `/etc/oracles` (`700`, files `600`) | **Home of the keys**, deliberately outside the repository |
| 4 | Install the systemd unit, **start only if credentials are complete** | Boot autostart + crash restart. If credentials are missing, install but don't start (see below) |
| 5 | Run `doctor` once | Confirm every account authenticates, so you know at install time whether it works |

> **Why it doesn't start without credentials**: the unit uses `Restart=always` + `RestartSec=10`.
> With an empty token the process exits immediately, systemd restarts it every 10 seconds →
> an infinite crash loop flooding the logs, and the script would report failure on its
> "did the service come up" check and `exit 1`, making it look like the whole install broke
> — when in fact the code, dependencies, config and unit are all in place, missing only two lines of credentials.
> So the script first validates the **shape** of the credentials (token must look like `<digits>:<base64ish>`, user IDs must be numeric),
> and only then runs `enable --now`; otherwise it prints "you're only two lines of credentials away" and exits cleanly.
> Note that "non-empty" is not a valid check here — the placeholder text in `oracles.env.example` is itself non-empty.

Fill in the config after installing. If credentials **were already filled in**, `install.sh` starts the service
directly and `doctor` runs as part of it; if you install first and configure later, start it manually:

```bash
sudo nano /etc/oracles/accounts.json    # account list
sudo nano /etc/oracles/oracles.env      # bot token + allow-list
sudo systemctl enable --now oracles-bot
```

> There's a safety net around the rsync exclude rules: after syncing, the script verifies that
> `accounts.example.json` / `pyproject.toml` / the unit file actually landed, and fails loudly if not.
> Exclude rules are easy to over-apply — `--exclude 'accounts.*.json'` would also exclude `accounts.example.json`.

### Running locally (no systemd)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e .

cp accounts.example.json ~/.oracles/accounts.json   # fill in real credentials
cp .env.example .env                                 # fill in the bot token
chmod 600 ~/.oracles/accounts.json

python -m oracles.cli doctor    # self-check first
python -m oracles              # start the bot
```

### Self-check first, then enable writes

```bash
$ python -m oracles.cli doctor
🩺 Environment self-check
  Config dir:  /etc/oracles
  Accounts:    17
  Writes:      disabled (DRY-RUN)

  ✅ [1] us-sanjose-1             1 availability domain
  ✅ [2] us-sanjose-1             1 availability domain
  ...
Result: 17 OK, 0 failed
```

Once that's clean, tap a few buttons in Telegram to review the plans, then set `ORACLES_WRITE_ENABLED=true`.

---

## The scripts in this repo

All live under `scripts/` and `deploy/`, and all can be run standalone.

| Script | Purpose | Key traits |
| --- | --- | --- |
| `deploy/install.sh` | One-command deploy to a VPS | Idempotent; scans for secrets and checks `ensurepip` before taking off; starts only when credentials are complete; runs `doctor` at the end |
| `scripts/selftest.py` | **Full read-only rehearsal** | 9 stages, **never performs a single write** — it only calls `plan_*` and prints plans, then verifies the write switches really do block. Assertion count scales with account count: ~40 for 17 accounts, 25 for 3 |
| `scripts/check_secrets.sh` | Pre-commit secret scan | `--verify` self-checks rule validity; `--staged` scans only the index; installable as a pre-commit hook. **The primary CI gate** |
| `scripts/import_legacy_accounts.py` | Migrate a legacy account pool | Converts "account list + shared `si.pem`" into this project's layout, setting 600/700 permissions |
| `scripts/audit_open_source.py` | **Pre-open-source audit** | `--verify` injects 12 synthetic probes to prove every rule fires; 11 credential-shape rules; tracked-file checks; `.gitignore` coverage measured with `git check-ignore` (not read from the file); real identifiers in docs (instance names / host aliases / public IPs). **The CI gate for the publish surface** |
| `.gitleaks.toml` | Precise exemptions for gitleaks | gitleaks in CI is a **supplementary net** (measured: it covers 1 of 7 credential classes); this config translates the inline `secret-scan:allow` marker it doesn't understand |

**Run these three after changing code**:

```bash
python scripts/selftest.py        # read-only rehearsal against your real account pool (~5 min for 17 accounts)
pytest tests/                     # 622 unit/integration tests (~25 s)
./scripts/check_secrets.sh        # make sure you didn't bring secrets along
python scripts/audit_open_source.py   # make sure you didn't bring anything un-publishable along
```

> ⚠️ **Don't delegate open-source security to a third-party scanner.** Injecting 7 classes of this
> project's real credentials, `check_secrets.sh` caught **7/7** while gitleaks caught only **1/7**
> — it doesn't recognise vendor prefixes like `ocid1.`, nor Telegram's `<digits>:<35 chars>` shape.
> See [pitfall #35](docs/06-踩坑清单.md).

### About `check_secrets.sh --verify`

Secret scanners have an **easily overlooked failure mode: they break and you can't tell**.
A rule that matches nothing and a rule that works fine but found nothing look identical in the output.

This project has been burned twice:

- a regex used `\b` — BSD grep on macOS doesn't support it in ERE mode, so the check **silently did nothing**
- the allow-list contained `aaaa` — but real OCID random segments all start with `aaaa`, making the OCID check **worthless**

So every rule must match its own sample, and the allow-list must not excuse real characteristics:

```bash
$ ./scripts/check_secrets.sh --verify
🔬 Rule self-check (every rule must match what it's meant to match)
  ✓ OCID      ✓ PEM key      ✓ API fingerprint      ✓ TelegramToken
  ✓ S3AccessKey      ✓ key filenames      ✓ suspected SecretKey
🔬 Allow-list self-check (real characteristics must not be excused)  ✓ × 7
🔬 Skip-list self-check (must not be so broad it exempts source too)  ✓ × 8
✅ All rules valid, allow-list doesn't excuse anything, skip-list scoped correctly.
```

**Always run `--verify` after editing this script.**

---

## Command reference

| Command | Description |
| --- | --- |
| `/start` `/help` | Main menu / command manual |
| `/status` | Runtime status and switches |
| `/a` | Account list |
| `/q` `/q 3` | Quota overview / for one account |
| `/i` `/i 3` | Instance overview / for one account |
| `/launch 3` | Create an instance (auto-picks the AD) |
| `/start_vm 3 <name>` | Start |
| `/stop_vm 3 <name>` | Stop |
| `/reboot 3 <name>` | Reboot |
| `/terminate 3 <name>` | Terminate (needs confirmation + destructive switch) |
| `/b 3` | Bucket list |
| `/obj 3 <bucket>` | List objects in a bucket |
| `/rmbucket 3 <bucket>` | Delete a bucket |
| `/s3key 3` | Issue an S3-compatible key |
| `/audit` | Billing-residue audit |
| `/security` | Security exposure scan |
| `/harden 3 <CIDR>` | Tighten port 22 source range |
| `/cancel` | Cancel a pending confirmation |

Instance names support **unique-prefix matching** — if a prefix matches several instances it errors out and asks you to be specific; it **never guesses**.

### Read-only CLI

```bash
python -m oracles.cli doctor                  # environment + auth self-check
python -m oracles.cli accounts                # account list
python -m oracles.cli quota --json out.json   # quota (JSON output available)
python -m oracles.cli instances --account 3   # instances
python -m oracles.cli buckets --stats         # buckets (object count / size / PARs)
python -m oracles.cli orphans                 # orphan volumes
python -m oracles.cli audit --account 11      # billing residue
python -m oracles.cli security                # security exposure
```

The CLI is deliberately **read-only**: a command line has no confirmation step — one Enter and it's done, which is far too easy to get wrong.
All write operations go through Telegram.

---

## Project layout

```
oracles/
├── oracles/
│   ├── config.py         config loading (config dir outside the repo + central validation at startup)
│   ├── oci_gateway.py    ★ the only place in the project that imports oci
│   ├── redact.py         redaction (last line of defence for logs and messages)
│   ├── models.py         data models and constants
│   ├── cloudinit.py      ★ cloud-init generation (username/password injection, so you can log in without a key)
│   ├── cli.py            read-only ops CLI
│   ├── services/
│   │   ├── quota.py      ★ per-AD quota and launch headroom
│   │   ├── compute.py    instances: plan_* read-only plans / execute_* execution + rescue console + block volumes
│   │   ├── storage.py    buckets, objects, S3 keys
│   │   ├── audit.py      orphan volumes, billing residue
│   │   ├── security.py   exposure scan, port-22 hardening (auto-backup before changes)
│   │   ├── accounts_mgmt.py ★ account CRUD + credential health check + hot reload
│   │   └── grab.py       ★ auto-hunt engine (interval loop until N instances or manual stop)
│   └── bot/
│       ├── app.py        bootstrap
│       ├── handlers.py   command and button routing (launch wizard state machine, config/volume/task management)
│       ├── dispatch.py   ★ the single exit point for write operations (all write switches live here)
│       ├── keyboards.py  inline keyboards (seven menus + wizard steps + volume list + task list)
│       ├── render.py     text rendering
│       ├── store.py      short-token mapping + pending actions + awaited input + wizard state
│       └── tasks.py      ★ hunt-task registry (in-process singleton: start/stop/list)
├── deploy/               systemd unit + one-command installer
├── docs/                 detailed documentation (Chinese)
├── scripts/              self-test, secret scan, legacy import, open-source audit
├── .gitleaks.toml        precise exemptions for gitleaks in CI (it doesn't know secret-scan:allow)
├── conftest.py           adds the repo root to sys.path (so bare pytest works)
└── tests/                622 tests (21 files)
    ├── telegram_harness.py     fake Update/Context so handlers can run offline
    ├── test_bot_handlers.py    handler-layer cases (allow-list / write switches / routing / rendering / logging)
    ├── test_accounts_mgmt.py   account list CRUD + **backup before write** (incl. end-to-end button flow)
    └── test_ci_guards.py       CI guard cases (step order, gitleaks config must not become a rubber stamp)
```

### Tests come in four layers

| Layer | What it tests | Why it can't be skipped |
| --- | --- | --- |
| Pure functions | redaction rules, port-coverage decisions, storage pagination, token store | Fast, but can't cover "wiring" |
| Render semantics | `[]` (genuinely none) vs `None` (query failed) must be distinguished | These errors are **silent** — only an assertion can pin them down |
| **Handler layer** | the allow-list blocks every command, read-only mode blocks every write, button routing | A **security property** — reading the code isn't convincing, it must actually run |
| **CI guards** | workflow step order, gitleaks config must not degrade into a rubber stamp | These two files only ever run on GitHub, **nobody clicks them locally**, so they rot silently |

The handler layer uses the fake `Update`/`Context` in `tests/telegram_harness.py`;
it never talks to Telegram and never touches OCI (OCI calls are monkeypatched), so 166 cases run in **2.5 seconds**.

The CI guard layer (`tests/test_ci_guards.py`) uses plain-text assertions, **without yaml / tomllib**
— because CI's Python 3.10 environment only has pytest + ruff. It pins down regressions that
"wouldn't turn any existing test red":

- moving gitleaks before the primary gate → one false positive and the primary gate never runs again
- inlining a copy of the rule table into the workflow YAML → drift from the local version (this project has been burned)
- adding a generic `example` allow-list entry to `.gitleaks.toml` → the whole scanner becomes a rubber stamp

Those 23 assertions are themselves verified with **mutation testing**: each of the regressions above
is deliberately introduced to confirm they really do turn red (6/6 caught).

---

## Deployment

```bash
sudo ./deploy/install.sh
```

The script: scans for secrets + checks `ensurepip` → creates the `oracles` system user → syncs code to `/opt/oracles`
→ creates the venv and installs dependencies → prepares `/etc/oracles` (mode 600) → installs the systemd unit
(**starts only when credentials are complete**) → runs `doctor` once.
It's **idempotent**: re-running only updates code and dependencies, never overwrites config.

The systemd unit ships a fairly strict sandbox (`ProtectSystem=strict`, `ProtectHome=read-only`,
`NoNewPrivileges`) because this process holds every cloud credential.

```bash
systemctl status oracles-bot
journalctl -u oracles-bot -f
```

See [docs/04-部署到VPS.md](docs/04-部署到VPS.md) (Chinese).

---

## Documentation

The detailed docs are currently **Chinese only**; this README is the English entry point.
See [docs/README.en.md](docs/README.en.md) for an English guide to what each document covers.

| Doc | Content |
| --- | --- |
| [01-快速开始](docs/01-快速开始.md) | From zero to running |
| [02-账号与密钥配置](docs/02-账号与密钥配置.md) | Multi-account setup, instance principal, key rotation |
| [03-命令手册](docs/03-命令手册.md) | Every command and interaction flow |
| [04-部署到VPS](docs/04-部署到VPS.md) | systemd, logs, upgrades, troubleshooting |
| [05-安全与开源注意事项](docs/05-安全与开源注意事项.md) | **Checklist before you push** |
| [06-踩坑清单](docs/06-踩坑清单.md) | 64 pitfalls measured in production, ordered by danger (26 of them are "meta-pitfalls" that make checks fail silently) |

---

## Dependencies

- Python ≥ 3.10
- [`oci`](https://github.com/oracle/oci-python-sdk) — the official Oracle SDK
- [`python-telegram-bot`](https://github.com/python-telegram-bot/python-telegram-bot) ≥ 21
- Optional: `boto3` (if you only want to talk to Object Storage via the S3-compatible API)

## License

MIT
