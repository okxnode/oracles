# Documentation — English guide

The detailed documentation in this repository is written in **Chinese**. This page is the
English map: it tells you what each document covers, which parts you actually need, and
where to look when you're stuck.

Everything here is a **guide to the Chinese docs**, not a translation of them.
For a full English description of the project itself, start with the
[root README](../README.en.md).

---

## Read in this order

Pick the path that matches what you're doing.

| I want to… | Read |
| --- | --- |
| **Get a bot running today** | [01-快速开始](01-快速开始.md) → [02-账号与密钥配置](02-账号与密钥配置.md) → [03-命令手册](03-命令手册.md) |
| **Deploy to a VPS and keep it alive** | [04-部署到VPS](04-部署到VPS.md) (then [01](01-快速开始.md) for the credential setup it assumes) |
| **Publish this to GitHub myself** | [05-安全与开源注意事项](05-安全与开源注意事项.md) — it's a checklist, read it *before* you push, not after |
| **Understand why the code is written this way** | [06-踩坑清单](06-踩坑清单.md) — every design decision traces back to a measured failure in here |
| **Just make it work / something is broken** | [06-踩坑清单](06-踩坑清单.md) 一级 + 二级, then [04-部署到VPS](04-部署到VPS.md) → 故障排查 |

---

## The documents

### [01-快速开始.md](01-快速开始.md) — Quick start

Zero to a running bot, roughly 20 minutes.

- **0.** What you need (a VPS, Python ≥ 3.10, a Telegram account, at least one OCI account)
  — with the tip that running the bot *on* one of your existing OCI free-tier instances is
  the cheapest option, since API latency is near-zero and it costs nothing extra
- **1.** Create the Telegram bot via **@BotFather**, get the token, get your numeric user ID via **@userinfobot**
- **2.** Get OCI API credentials — repeated **per account**: Console → avatar → *My profile* → *API keys* → *Add API key* → *Generate API key pair*. Includes a sample `[DEFAULT]` profile block and the two facts people get wrong:
  - the **private key can be shared by every account** — add it once
  - the **private key is downloadable exactly once**; close the page and it's gone
- **3.** Install — **Method A** one-command systemd deploy (recommended) or **Method B** local manual run for debugging
- **4.** Fill in config: `accounts.json` (account list) and the environment variables
- **5.** `doctor` self-check
- **6.** Run the full rehearsal — `scripts/selftest.py`
- **7.** Start the bot (systemd or locally)
- **8.** Only enable write permissions **after** you're comfortable

### [02-账号与密钥配置.md](02-账号与密钥配置.md) — Accounts and key configuration

Everything about where credentials live and how they're shaped.

- **Where the config lives** — `$ORACLES_HOME` (default `~/.oracles`, `/etc/oracles` in production). The point: **outside the repository tree**, so keys can't reach git by accident
- **`accounts.json`** — every field, plus the validation rules the loader enforces at startup
- **Multi-account: how to place private keys** — **Plan A** one shared key for all accounts (recommended) vs **Plan B** one key per account; and the `600` permission requirement
- **Instance principal** — for when the bot runs *on* an OCI instance: create a dynamic group, write the policy, configure it
- **Environment variables** — required / switches / optional, plus **load precedence**
- **Key rotation** — rotating an OCI API key, the Telegram bot token, and S3-compatible keys
- **FAQ**

### [03-命令手册.md](03-命令手册.md) — Command manual

The interaction reference.

- **How to interact** — the bot is button-driven; commands are shortcuts
- **Command list** by area: overview / quota / instances / buckets / audit / security
- **Button flows** — the **step-by-step launch wizard**, the instance action menu, the volume management menu, and what "back" does at each level
- **Read-only CLI** — `doctor`, `accounts`, `quota`, `instances`, `buckets`, `orphans`, `audit`, `security`; plus a **scheduled-inspection example** (write a quota snapshot to a file every day at 09:00 and alert on it)
- **Full rehearsal** — `scripts/selftest.py`

### [04-部署到VPS.md](04-部署到VPS.md) — Deploying to a VPS

- **Prerequisites**, then **one-command deploy** and **manual deploy** (user + directories, venv, config dir, config, systemd)
- **The systemd unit explained** — including the hardening block (`ProtectSystem=strict`, `ProtectHome=read-only`, `NoNewPrivileges`) and *why* a process holding every cloud credential gets a sandbox
- **Day-to-day operations** — status, live logs, last 100 lines, errors only, restart (required after editing `oracles.env` or `accounts.json`), stop
- **Upgrading**
- **Troubleshooting** — service won't start / bot doesn't reply / one account fails to query / `LimitExceeded` on launch / `OutOfHostCapacity` on launch / buttons stop responding after a restart
- **Security recommendations** — don't expose the bot publicly, tighten SSH (audit port 22, narrow to a fixed IP, one account at a time), run read-only for a while, **flip dangerous switches on only when needed and off right after**, run the bot under a dedicated account, back up the config
- **Resource footprint**

### [05-安全与开源注意事项.md](05-安全与开源注意事项.md) — Security and open-sourcing notes

**Read this before you push anything.** It is the checklist that produced the guards in this repo.

- **I. Pre-push checklist** — scan the workspace, confirm no sensitive file is tracked, read what's actually staged. Covers installing the scanner as a **pre-commit hook** and the **CI-side net**; explains the `secret-scan:allow` inline exemption marker and the placeholder allow-list
- **II. What has already been done** — architectural key isolation, `.gitignore`, redaction (`oracles/redact.py`), log noise reduction, **allow-list that denies by default**
- **III. The layers protecting write operations** — layer 4 *structural DRY-RUN*, layer 5 *live re-verification before execution*, layer 6 *proactive interception*, and the **single exit point for all writes**
- **IV. If you've already leaked something** — revoke the OCI API key, the Telegram bot token, the S3-compatible key; check whether anything was tampered with; optionally scrub git history (`git-filter-repo`)
- **V. Letting other people use this project** — including what not to paste into issues and PRs
- **VI. What this project deliberately does not do**
- **VII. Dependency security** — known-vulnerability scanning and supply chain

### [06-踩坑清单.md](06-踩坑清单.md) — Pitfall list

**64 pitfalls actually hit in production**, ordered by danger rather than by date.

> ⚠️ **The numbering is chronological, the sections are by danger level.**
> New entries are appended to the **end of their section**, so the numbers in the body are
> **out of order** (e.g. #41–#44 sit between #20 and #21). If you assert on numbers, sort first.
> Entries marked 🔬 are specific to using the official SDK (behaviour differs from the CLI era).

| Section | What's in it |
| --- | --- |
| 🔴 **Tier 1 — data loss or ongoing billing** | Free-tier quota is bound to a *single availability domain*, not the region · `bootVolumeQuota` reports **block storage** exhaustion, not CPU · `list_objects` returns a `ListObjects` model, not a list · object-listing pagination cursor lives in the **response body** (`next_start_with`), so a generic `opc-next-page` paginator silently truncates at 1000 objects · "empty bucket" and "query failed" must be two different results · deleting a bucket is blocked by `PreauthenticatedRequestStillExists` (409) · deleting an instance *inside a pool* is a no-op · boot-volume behaviour on terminate is **opposite between SDK/CLI and the console** |
| 🟠 **Tier 2 — feature failure or misjudgement** | Limit names (`standard-e2-micro-core-count`), `resource_availability` needing an AD for AD-level limits, AD names carrying random prefixes **with inconsistent case**, instance-pool subcommands, S3 key propagation delay, `botocore ≥ 1.36` aws-chunked being unsupported by OCI, `update_security_list` being a **full replace**, `metadata` being an explicit `null`, `oci.retry` having no `RetryStrategy` class, and the whole cluster of cloud-init/`sshd` traps (#41–#49) |
| 🟡 **Tier 3 — experience or confusion** | Things that work but bite you: wording, ordering, defaults |
| 🧯 **Meta-pitfalls (26)** | Not OCI traps — traps that **break the checks themselves**: a regex using `\b` under BSD grep ERE (silently matches nothing), an allow-list entry `aaaa` when real OCID segments start with `aaaa` (the check becomes worthless), stubs placed so high they stub out the behaviour under test, tests whose default value makes them never exit, audit scripts that become the leak source. **Business bugs are visible; meta-bugs are not — read this section at least once.** |
| **Appendix** | Two commonly confused concept pairs |
| **How these were found** | The measurement method behind the list |

---

## Where do I find…

| Question | Document | Section |
| --- | --- | --- |
| What does the bot token look like / where do I get it | 01 | 1 |
| Which four values go into `accounts.json` | 01 → 02 | 2 / `accounts.json` |
| Can all accounts share one private key | 01 / 02 | 2 / Plan A |
| Where do keys live on disk, and with what permissions | 02 | Config location / Permissions |
| Which environment variables exist and which one wins | 02 | Environment variables |
| How do I rotate a leaked credential | 02 / 05 | Key rotation / IV |
| How do I make the bot start on boot | 04 | One-command deploy / systemd unit |
| How do I read the logs | 04 | Day-to-day operations |
| The service starts and immediately dies | 04 | Troubleshooting → service won't start |
| Launch fails with `LimitExceeded` or `OutOfHostCapacity` | 04 / 06 | Troubleshooting / #1, #2, #44 |
| Why does the bot refuse to delete this volume | 06 | Tier 1 + the volume-safety entries |
| What exactly gets scanned before a push | 05 | I / `scripts/check_secrets.sh` |
| Which buttons exist and what they do | 03 | Button flows |
| Can I automate quota checks | 03 | Read-only CLI → scheduled inspection |
| Why is the CLI read-only | Root README | Command reference |

---

## Language

- **`README.en.md`** (repo root) — full English project description: what it does, the security design, the per-AD quota trap, install, scripts, commands, layout, tests, deployment
- **`docs/README.en.md`** (this file) — English guide to the Chinese documents below
- **`docs/01`–`docs/06`** — Chinese, and considerably more detailed than either English page

If you'd like a specific document translated, open an issue naming it — the docs are
more likely to get translated in the order people actually ask for.
