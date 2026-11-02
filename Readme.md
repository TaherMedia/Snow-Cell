# Snow Cell on GitHub Actions: Windows 2025 RDP + Cloudflare R2 + five Telegram bots

Runbook for the workflow **"🪟 Windows 2025 Server TEMP RDP"**. It covers the files, the secrets,
install and test commands, how one session runs, the auto-restart chain, the 355-versus-359-minute
problem, a verification checklist and troubleshooting.

---

## 0. Read this first

**One decision is open (section 8).** You asked for a 355-minute session *and* a 359-minute job limit.
Those two numbers cannot both hold once the setup time and the shutdown work are counted. The shipped
default keeps your numbers exactly as written and relies on a safety net; section 8 explains the risk
and lists the options. Nothing was reduced without your say-so.

**What was verified and what was not.**

| Verified here (136 offline tests, all passing) | Not verifiable from here |
|---|---|
| Whole `setup.py` lifecycle against a fake S3: download, SHA-256, safe extract, 5 concurrent bots, one shared deadline, graceful stop, rebuild, compare, delete, upload, verify, restore | Execution on a real `windows-latest` runner (PowerShell steps are only structure-linted) |
| Crash recovery, cancellation, job-timeout and lost-response paths; refused-checksum fallback | How GitHub treats `if: always()` steps after a **job timeout** (documented for cancellation, not stated for timeouts) |
| Archive safety (traversal, absolute paths, symlinks, bombs, duplicates, reserved names) | Real Cloudflare R2 behaviour and your token's permissions |
| Secret isolation: R2/RDP/Tailscale values never reach the bots, logs, ZIP or command lines | WhatsApp on Windows Server (no Microsoft Store; a WhatsApp Web shortcut is the fallback) |
| Workflow invariants: your original steps byte-identical, step ids/outputs, restart conditions | Telegram/WhatsApp installers running in a background logon |

**Defaults you may want to change.** Bots live in `C:\Users\ATOMA\Downloads\snowcell\<Bot>` (set the
variable `SNOWCELL_BOT_ROOT=C:\Users\ATOMA\Downloads` to put the five folders directly in Downloads);
the replace policy is the literal one (any SHA-256 difference replaces the object); a crashed bot's last
20 log lines are echoed into the Actions log (set `SNOWCELL_CRASH_TAIL_LINES=0` on a public repository).

---

## 1. Files and exact paths

Put these in the repository root exactly as shown.

| Path | Purpose | Required |
|---|---|---|
| `.github/workflows/setup.yml` | The workflow (replaces your current one) | yes |
| `scripts/setup.py` | Lifecycle runner: validate, download, extract, run bots, stop, rebuild, compare, replace | yes |
| `scripts/bot_runner.py` | Launcher for each `bot.py`; turns the Windows stop signal into a clean Ctrl+C | yes |
| `.github/workflows/watchdog.yml` | Optional cron safety net for the restart chain | no |
| `tests/` (`test_setup.py`, `test_workflow.py`, `ps_lint.py`, `fixtures/`, `fakelibs/`) | Offline tests; never used by the workflow | no |
| `docs/RUNBOOK.md` | This file | no |

The workflow checks out only the `scripts` folder (sparse checkout), so tests and docs add no setup time.

Your bots are **not** in the repository: they come from `snowcell.zip` in R2 at the start of every
session, and the updated archive goes back at the end.

**Archive layout that `snowcell.zip` must have** (five folders at the top level, no wrapper folder):

```
snowcell.zip
  SnowTempbot/bot.py     (+ Data/, Sessions/ and anything else the bot owns)
  SnowMaxbot/bot.py
  SnowKeeperbot/bot.py
  SnowiOSbot/bot.py
  SnowCellbot/bot.py
```

A missing `bot.py`, an absolute path, `..`, a symlink, an encrypted entry, a duplicate or
case-colliding name, a Windows-reserved name or an oversize unpack makes the run stop with exit code 13
before anything is started.

---

## 2. Secrets and variables

Settings, Secrets and variables, Actions.

### 2.1 Secrets

| Name | Status | Who receives it |
|---|---|---|
| `RDP_PASSWORD` | existing | job-level `env` (as before). **Removed from the five bots' environment.** |
| `TAILSCALE_AUTHKEY` | existing | job-level `env` (as before). **Removed from the five bots' environment.** |
| `R2_ACCOUNT_ID` | new | only the three `python scripts/setup.py` steps (check, run, recover) |
| `R2_ACCESS_KEY_ID` | new | same three steps |
| `R2_SECRET_ACCESS_KEY` | new | same three steps |
| `R2_ENDPOINT` | new | same three steps |
| `R2_TOKEN_VALUE` | new | **nobody.** Stored as requested, never referenced by any step. |
| `SNOWCELL_DISPATCH_TOKEN` | optional | only the restart job and the watchdog, and only if your organisation forbids `actions: write` for the built-in token (section 7) |

Why `R2_TOKEN_VALUE` is not passed: the S3 API signs requests with the access key pair. Cloudflare
derives that pair from the token (Access Key ID = token ID, Secret Access Key = SHA-256 of the token
value), so the token value itself is not needed for any call this project makes. Keeping it out of every
process is the safer reading of "stored but not exposed".

Set them without putting values in your shell history (`gh` prompts for the value):

```powershell
gh secret set R2_ACCOUNT_ID        --repo OWNER/REPO
gh secret set R2_ACCESS_KEY_ID     --repo OWNER/REPO
gh secret set R2_SECRET_ACCESS_KEY --repo OWNER/REPO
gh secret set R2_ENDPOINT          --repo OWNER/REPO
gh secret set R2_TOKEN_VALUE       --repo OWNER/REPO
```

### 2.2 Variables (required)

| Name | Value |
|---|---|
| `R2_BUCKET_NAME` | `snowcell` |
| `R2_OBJECT_KEY` | `snowcell.zip` |

```powershell
gh variable set R2_BUCKET_NAME --body snowcell      --repo OWNER/REPO
gh variable set R2_OBJECT_KEY  --body snowcell.zip  --repo OWNER/REPO
```

`setup.py` refuses to run (exit 10) with any other bucket or key: the exact names are hard-wired as a
guard, so no `snowcell-new.zip`, `-backup` or timestamped key can ever be produced.

### 2.3 Variables (optional, all have safe defaults)

| Name | Default | Effect |
|---|---|---|
| `SNOWCELL_DEADLINE_MODE` | `bot-start` | `bot-start`: the bots run `session_minutes` from the moment all five are running (literal). `job-anchored`: the runtime is shortened so that 5 minutes remain before the 359-minute job limit (section 8, option B). |
| `SNOWCELL_SHUTDOWN_GRACE_SECONDS` | `45` | Wait per bot for a graceful stop before terminate, then kill (1 to 600). |
| `SNOWCELL_FAILURE_POLICY` | `continue` | `continue`: a crashed bot does not stop the other four. `abort`: the first crash ends the session (data is still saved). |
| `SNOWCELL_REPLACE_POLICY` | `bytes` | `bytes`: any SHA-256 difference replaces the object (literal reading of your rule). `content`: replace only when a file's content changed; ZIP-container-only differences are ignored. |
| `SNOWCELL_BOT_ROOT` | `<Downloads>\snowcell` | Where the five bot folders are extracted. Never deletes anything; it refuses (exit 13) if a bot folder already exists there. |
| `SNOWCELL_RESCUE_SECONDS` | `600` | How long a *missing* object is retried (restore or upload) before giving up. Always bounded by the job time that is left. |
| `SNOWCELL_HEARTBEAT_SECONDS` | `60` | Interval of the "session active; N minutes remaining" line (same as your old loop). |
| `SNOWCELL_CRASH_TAIL_LINES` | `20` | Lines of a crashed bot's log echoed into the Actions log. `0` disables. |
| `SNOWCELL_AUTORESTART` | on | `false` switches the whole restart chain off (restart job and watchdog). |
| `SNOWCELL_MIN_RESTART_MINUTES` | `30` | A run shorter than this never queues a successor (loop guard). |
| `SNOWCELL_WATCHDOG` | on | `false` disables only the watchdog. |
| `SNOWCELL_WATCHDOG_GRACE_MINUTES` | `10` | The watchdog waits this long after a run ended before stepping in. |
| `SNOWCELL_SESSION_MINUTES` | `355` | `session_minutes` the watchdog passes when it starts a run. |
| `SNOWCELL_WORKFLOW_FILE` | `setup.yml` | File name of the main workflow, used by the watchdog. |

Knobs that exist in `setup.py` but are **not** wired into the workflow (add a line to the step `env:`
if you need one): `SNOWCELL_POST_RESERVE_MINUTES` (5, used by `job-anchored`),
`SNOWCELL_PUBLISH_SAFETY_SECONDS` (45), `SNOWCELL_MAX_UNPACKED_MIB` (8192), `SNOWCELL_BOT_LAUNCH`
(`wrapper`), `SNOWCELL_LOG_DIR`, `SNOWCELL_WORK_DIR`, `SNOWCELL_JOB_START_SKEW_SECONDS` (60),
`SNOWCELL_APP_STATUS_FILE` (default `C:\ProgramData\ATOMA-AppSetup\status.txt`, the file whose changes the
heartbeat prints).

Set by the workflow itself, not by you: `SNOWCELL_JOB_START_EPOCH` (step 0), `SNOWCELL_JOB_TIMEOUT_MINUTES`
(`359`, must equal `timeout-minutes`) and `SNOWCELL_DOWNLOADS_DIR` (step 8b writes the real Downloads folder of
`ATOMA`, `C:\Users\ATOMA\Downloads`). `SNOWCELL_RUNTIME_SECONDS` is a test hook that overrides `session_minutes`;
never set it in the workflow.

---

## 3. Cloudflare R2

1. **Bucket**: `snowcell`, private. Leave the `r2.dev` public URL disabled and attach no public custom
   domain. Nothing in this project needs public access.
2. **API token**: R2, Manage API tokens, Create API token. Permission **Object Read & Write**, scoped to
   the bucket `snowcell` only. The page shows the token value, the Access Key ID and the Secret Access
   Key once; copy them into the five secrets above straight away.
3. **Endpoint** (`R2_ENDPOINT`), the bare S3 API address with no bucket name and no path:

   | Bucket location | Endpoint |
   |---|---|
   | no jurisdiction (the default, "Automatic") | `https://<ACCOUNT_ID>.r2.cloudflarestorage.com` |
   | EU | `https://<ACCOUNT_ID>.eu.r2.cloudflarestorage.com` |
   | FedRAMP | `https://<ACCOUNT_ID>.fedramp.r2.cloudflarestorage.com` |
   | US | `https://<ACCOUNT_ID>.us.r2.cloudflarestorage.com` |

   Cloudflare's documentation says that for a bucket in a jurisdiction you must use the jurisdiction
   endpoint, and that a jurisdiction endpoint cannot reach resources outside its jurisdiction. If you get
   `NoSuchBucket` for a bucket you can see in the dashboard, a wrong endpoint is the usual cause.
   `R2_ACCOUNT_ID` must be the same 32 hexadecimal characters that appear in the endpoint.
4. **First object**: upload your current `snowcell.zip` once (dashboard: the bucket, Objects, Upload).
   If the object is missing at the start of a session, `setup.py` stops with exit 12 and touches nothing.
5. **Keep an offline copy** of `snowcell.zip` somewhere that is not R2. The design never deletes the
   old object until the replacement is validated and a verified local copy of the original exists, but
   an offline copy is the only protection against everything else (a wrong token, a mistaken manual
   delete, a bug in your own bots).

Optional read-only check from your own computer (AWS CLI v2; the region is a placeholder R2 ignores):

```powershell
aws configure                       # Access Key ID, Secret Access Key, region: auto
aws s3api head-object --bucket snowcell --key snowcell.zip `
  --endpoint-url https://<ACCOUNT_ID>.r2.cloudflarestorage.com
```

If a recent AWS CLI or SDK is rejected with a checksum error, set `AWS_REQUEST_CHECKSUM_CALCULATION=WHEN_REQUIRED`
and `AWS_RESPONSE_CHECKSUM_VALIDATION=WHEN_REQUIRED` for that command. `setup.py` already uses the
equivalent boto3 settings and, if R2 ever refuses the SHA-256 checksum header on an upload, retries
once without it so a restore can never fail for that reason.

---

## 4. Install, test, first run

### 4.1 Put the files in place

```powershell
git checkout -b snowcell-setup
# copy the files to the paths in section 1, then:
git add .github/workflows scripts tests docs
git commit -m "Snow Cell: bots + R2 sync + auto-restart"
git push -u origin snowcell-setup
```

Your current `setup.yml` already exists on the default branch, so the Actions tab lists the workflow.
In the **Run workflow** dialog pick `snowcell-setup` under *Use workflow from* to test the new version
before merging. The restart job starts its successor on the same branch as the run it belongs to. A
scheduled workflow (the watchdog) runs only from the **default** branch, so merge before you rely on it.

### 4.2 Offline tests (nothing touches GitHub, R2 or Telegram)

```bash
python -m pip install pyyaml            # only for tests/test_workflow.py
python -m py_compile scripts/setup.py scripts/bot_runner.py
python -m unittest discover -s tests -v # 136 tests, about 3.5 minutes
```

The tests use a fake `boto3`; no real credentials are needed or read.

### 4.3 Configuration check on your own machine (placeholder values are fine)

```powershell
$env:R2_ACCOUNT_ID        = "0123456789abcdef0123456789abcdef"
$env:R2_ACCESS_KEY_ID     = "placeholder"
$env:R2_SECRET_ACCESS_KEY = "placeholder"
$env:R2_ENDPOINT          = "https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com"
$env:R2_BUCKET_NAME       = "snowcell"
$env:R2_OBJECT_KEY        = "snowcell.zip"
python scripts/setup.py --check-config
```

It prints the effective (non-secret) settings and the job-time budget, then
`R2 configuration is structurally valid`. The same check runs in the workflow before any long install,
so a missing secret fails the run in the first minute, not after the RDP setup.

### 4.4 First run (smoke test)

1. Keep an offline copy of `snowcell.zip` (section 3, step 5).
2. Actions, **🪟 Windows 2025 Server TEMP RDP**, Run workflow, `session_minutes` = **10**.
3. Watch for the lines listed in the verification checklist (section 10). If any file in the bot folders
   changed (the bots normally write to their `Data` and `Sessions` folders) you will see the full replace
   cycle: `Changes detected`, `Deleting the old object`, `uploaded ... and verified`. If nothing changed
   you will see `No changes detected` and the R2 object is left untouched.
4. Runs shorter than `SNOWCELL_MIN_RESTART_MINUTES` (30) never queue a successor, so the smoke test does
   not start a chain.
5. Only then run the default `355`.

To start the chain on purpose, run the workflow manually once. The restart job and the watchdog never
start the first session.

---

## 5. How a session runs

### 5.1 Workflow order (job `secure-rdp`)

| # | Step | Origin | What it does |
|---|---|---|---|
| 0 | ⏱️ Record job start time | new | Stores the job's start (unix seconds) in `SNOWCELL_JOB_START_EPOCH`, used for the job-time budget. |
| 1 | 🔐 Validate required secrets | yours | Unchanged. |
| 1b | 📥 Check out the bot runner scripts, 🐍 Set up Python 3.12, 🧪 Check configuration | new | Sparse checkout of `scripts/` only. `--check-config` fails the run within the first minute if an R2 secret or variable is missing or malformed. |
| 2 to 8 | RDP, ATOMA, Tailscale install and connect, RDP check, WARP install and connect | yours | **Byte-identical to your file** (a test compares them). |
| 8b | 📁 Prepare ATOMA Downloads folder (no login needed) | new | A scheduled task registered with ATOMA's password performs a real background logon. Windows creates `C:\Users\ATOMA` and `Downloads` exactly as the first RDP login would, so a later RDP session finds the same folders. |
| 9 | 📋 Display RDP connection details | yours, text changed | No longer says "log in as ATOMA"; shows the bots folder. |
| 10 | 👤 Configure app installation for ATOMA | yours plus a background task | Your first-logon task is still registered as a fallback. A second task, `ATOMA-AppSetup-Now`, starts the same installer immediately, so Telegram, WhatsApp and Chrome no longer wait for an RDP login. |
| 11 | 🤖 Run Snow Cell bots and sync snowcell.zip with R2 (`id: setup`) | replaces "Keep session alive" | `python scripts/setup.py`. A non-zero exit fails the step. |
| 12 | 🛟 Recover and publish (`id: recover`, `if: always()`) | new | `python scripts/setup.py --recover`. Does nothing when step 11 finished normally. |
| job `restart` | ♻️ Queue the next session | new | Section 7. |

Step 11 keeps what the old loop did: a "Session active; N minutes remaining" line every 60 seconds and an
`ATOMA app status:` line whenever the installer status changes.

**App installation without a login.** Telegram: WinGet first, then the official installer
(`/VERYSILENT /NORESTART`, a per-user install). Chrome: preinstalled on the runner image; it is only
updated. WhatsApp: the native app is published only through the Microsoft Store, which Windows Server
does not have, so the WinGet `msstore` install is attempted and, when it cannot work, a
**WhatsApp Web** app shortcut (`chrome --app=https://web.whatsapp.com/`) is created and the status file says
`SUCCESS (with notes)`. This path could not be executed here; check `C:\ProgramData\ATOMA-AppSetup\install.log`
after the first run.

### 5.2 What `setup.py` does, stage by stage

1. **Configure.** Validates the six R2 values, the endpoint format, the account id and the exact bucket and
   key. Every secret value is registered so that any accidental appearance in output is replaced by `***`.
   Exit 10 on any problem.
2. **Dependencies, in parallel with the download.** In a background thread, with the same interpreter the
   bots use (`sys.executable`, no shell):
   `python -m pip install "Telethon==1.44.0" pytz phonenumbers aiofiles aiosqlite boto3`.
   The exit code is checked (exit 14 on failure or after 15 minutes).
3. **Download.** `HEAD` first, then a streamed `GET` into the work folder in 1 MiB chunks, flushed and
   fsynced, size-checked against the object, SHA-256 computed while streaming. Network errors and
   429/5xx are retried (4 attempts); rejected credentials are **not** retried (exit 11); a missing object
   stops the run with nothing touched (exit 12); an incomplete transfer is exit 26. The logged
   `SHA-256` of the download is the baseline for the final comparison.
4. **Validate, then extract.** The whole archive is validated before a single file is written (section 1
   lists what is refused). Extraction is CRC-checked, SHA-256 is recorded per file, original file times are
   restored, and `Data` and `Sessions` folders are guaranteed for every bot. An existing bot folder is never
   overwritten. Exit 13 on any problem.
5. **Clean environment.** The bots get an environment without any credential (section 6).
6. **Launch five bots concurrently.** For each bot:
   `python -u scripts/bot_runner.py --stop-file <work>\stop-requests\<Bot>.stop <Bot>\bot.py`.
   Working directory is the bot's own folder, no shell, stdin closed, stdout and stderr appended to
   `<Downloads>\snowcell-logs\<Bot>.log`, each bot in its own process group. PIDs and exit codes are
   tracked. A 5-second start-up check reports bots that die at once; if none survive: exit 15.
7. **Run on ONE shared deadline.** All five share a single monotonic deadline: the moment the start-up
   check ends plus `session_minutes` (default 355). A crashed bot is reported once (exit code, uptime, last
   log lines) and, with the default `continue` policy, the other four keep running. If all five exit early
   the session ends early, the data is still saved, and the result is exit 16.
8. **Stop and confirm.** `CTRL_BREAK_EVENT` goes to each bot's process group; `bot_runner.py` turns it into
   the same `KeyboardInterrupt` that Ctrl+C causes, so the bot's own clean-up runs. If the signal cannot be
   delivered, a stop file watched by `bot_runner.py` does the same (never both). After the grace period
   (45 s) a leftover is terminated, then killed together with its child processes (`taskkill /T /F`).
   Finally a scan ends any process whose command line still mentions the bot folder. Unless every process
   is confirmed gone the run stops with exit 17 and **neither rebuilds the archive nor touches R2**. A
   2-second pause lets Windows release SQLite file handles.
9. **Rebuild and validate the ZIP.** Same top-level structure, `Data` and `Sessions` preserved, files in
   sorted order, directory timestamps fixed, so an unchanged tree produces identical bytes. Not archived:
   `__pycache__`, `*.pyc`, `*.pyo`, `.pytest_cache`, `Thumbs.db`, `desktop.ini`, `.DS_Store`, `__MACOSX`
   (never under `Data` or `Sessions`) and symbolic links. Runner files, logs, the work folder and every
   credential are outside the bot folders and are never written to disk by this project, so they cannot be
   in the ZIP. The result must open, pass a CRC test, pass the same validation as the download and match
   the folders file by file (exit 18 otherwise).
10. **Compare SHA-256 of the real bytes** (download versus rebuild). Identical: `No changes detected`, R2 is
    left untouched, exit 0.
11. **Replace**, only when the bytes differ and all of this holds: both local archives are re-hashed and
    intact (exit 19 otherwise); enough job time is left (otherwise exit 24 and R2 untouched); R2 still holds
    exactly the object that was downloaded (otherwise exit 25, "conflict": someone changed it, nothing is
    overwritten). Then, in this order:
    1. keep the verified local copy of the original;
    2. `DeleteObject`, then `HEAD` to confirm it is gone (exit 20 if it cannot be deleted; the old object
       is still there);
    3. immediately `PutObject` to the **same key** with `ChecksumSHA256`, so R2 verifies the hash while
       storing (3 attempts; if the header itself is refused the upload repeats without it);
    4. verify: `HEAD` for size and the server-side SHA-256 (ETag/MD5 as fallback), plus a **full
       re-download and SHA-256 comparison whenever the job-time budget allows it**;
    5. on failure restore the original from the verified local copy and verify that; if the object is
       missing, keep retrying for up to `SNOWCELL_RESCUE_SECONDS` (bounded by the job time left). Exit 21
       means R2 holds the original again (this session's changes are not saved). Exit 22 means the object
       is gone from R2 and the only copy is on the runner until the job ends (see section 11).
    An upload whose response was lost is recognised by looking at what R2 really holds, so a good
    replacement is never overwritten by the restore. If R2 cannot be reached during verification the
    replacement is left in place and the run is reported as failed; it is never blindly rolled back.
12. **Report.** Step summary table, outputs `exit_code` and `restart_ok`. The step exits non-zero when a bot
    crashed (16) or never started (15) even if the data was saved. **Exit 0 is only ever printed after a
    verified replacement or a proven "unchanged".**

### 5.3 Recovery step (`--recover`)

`setup.py` writes a small state file (atomic writes, updated after every phase). The `if: always()` step
reads it:

* the main step finished: nothing to do;
* it stopped before any bot started: nothing to save;
* otherwise (cancelled, timed out, killed): leftover bot processes are ended, the archive is rebuilt and
  published with the same code, within the time GitHub still allows.

GitHub's documented cancellation sequence is: SIGINT (Ctrl+C), 7.5 s, SIGTERM (Ctrl+Break on Windows),
2.5 s, then the process tree is killed; the remaining `always()` steps get at most **5 minutes** counted
from the cancel request. `setup.py` treats the first signal as "stop the bots now (grace shortened to 6 s)
and leave the archive and upload to the recovery step". A publish that cannot finish inside the remaining
window is **not started** (exit 24, R2 untouched).

---

## 6. Secret isolation

| Requirement | How it is met |
|---|---|
| Credentials only in GitHub Secrets | Nothing is stored in the repository, the ZIP, the bot sources or any report. |
| Passed only through step-level `env` | The four R2 secrets and two variables appear only in the `env:` of the check, run and recover steps. `R2_TOKEN_VALUE` appears in no `env:` at all. |
| Never logged, printed, echoed or dumped | No code path prints the environment. All secret values are registered and masked as `***` in every line `setup.py` prints, including tracebacks and the step summary. The start-up log names the *variables removed* from the bot environment, never their values. |
| Never on a command line | boto3 receives the keys as in-process arguments. `pip`, the bots, `taskkill` and the stray-process scan carry no secret; the scan reads the bot folder from an environment variable instead of its command line. |
| No `shell=True` | Every subprocess uses an argument list. |
| Helper programs never receive them either | `pip` (which can run package build code), `taskkill` and the PowerShell process scan are started with the same credential-free environment as the bots. Only `setup.py` itself holds the R2 keys. |
| Bots never receive infrastructure secrets | `sanitized_env` removes `RDP_PASSWORD`, `TAILSCALE_AUTHKEY`, `GITHUB_TOKEN`, `GH_TOKEN`, every variable starting with `R2_`, `AWS_`, `CLOUDFLARE_`, `CF_`, `ACTIONS_` or `SNOWCELL_`, every variable whose name contains `PASSWORD`, `PASSWD`, `SECRET`, `TOKEN`, `AUTHKEY`, `AUTH_KEY`, `API_KEY`, `PRIVATE_KEY`, `CREDENTIAL` or `ACCESS_KEY`, and any variable whose *value* contains a registered secret. Ordinary Windows and Python variables (`PATH`, `USERPROFILE`, `TEMP`, `SystemRoot` and so on) are kept. A second check stops the run if an `R2_*` or `AWS_*` variable somehow survives. |
| Telegram session contents never logged | Every file in the bot folders is handled as opaque bytes (hashed and zipped). Logs contain file counts, sizes and archive SHA-256 values only. |
| Bucket stays private | The code never sets ACLs, public access, presigned URLs or CORS. |

Things to know:

* If one of your bots reads its own settings from an environment variable whose name matches the patterns
  above, it will not receive it. Bots started this way before had no such variable from the workflow
  either; their settings come from files inside the ZIP.
* A crashed bot's last log lines are copied into the Actions log (default 20 lines). That text is the
  bot's own output, which this project cannot vet. On a public repository set
  `SNOWCELL_CRASH_TAIL_LINES=0`.
* **This is environment isolation, not an operating-system sandbox.** The bots run as the same Windows
  user as the runner step, and that user is an administrator. Code running as an administrator can in
  principle read other processes. The design stops *accidental and inherited* exposure; it cannot defend
  against malicious bot code. Treat the code inside `snowcell.zip` as trusted.
* Your original Tailscale step passes the auth key to `tailscale up` on its command line (GitHub masks it
  in the log, but it is visible in the process list while that command runs). It was left unchanged because
  you asked to preserve existing functionality. The Tailscale CLI documents a file form,
  `--auth-key=file:<path>`, if you want to remove that exposure later.

---

## 7. Auto-restart

### 7.1 What is possible

You asked for a new runner start every 356 minutes. Cron cannot express that: its fields are minutes,
hours, days, months and weekdays, 356 minutes (5 h 56 min) does not divide a day, scheduled runs are
best-effort and may be delayed or dropped, and they run only from the default branch. A schedule is
therefore the wrong primary mechanism.

The design is **event-chained**: each session queues its own successor, and an optional cron watchdog only
repairs a broken chain.

### 7.2 The restart job (primary)

Job `restart`: `needs: secure-rdp`, Ubuntu, 10-minute limit,
`if: !cancelled() && vars.SNOWCELL_AUTORESTART != 'false' && needs.secure-rdp.outputs.restart_ok == 'true'`.
In order:

1. Derives the workflow file name from `github.workflow_ref` (so renaming `setup.yml` is safe).
2. **Loop guard.** The run must have lasted at least `SNOWCELL_MIN_RESTART_MINUTES` (30). A run that failed in
   its first minutes (bad secret, bad token) never queues a successor.
3. **Overlap guard.** If another run of the workflow is queued or in progress, it does nothing.
4. `gh workflow run <file> --ref <this branch> -f session_minutes=<same value as this run>`.

`restart_ok` is decided by `setup.py` from the exit code:

| Restart allowed | Needs a person (chain stops) |
|---|---|
| 0 OK, 1 UNEXPECTED, 16 BOT_CRASH, 17 SHUTDOWN, 18 ZIP_BUILD, 19 HASH, 20 R2_DELETE, 21 R2_UPLOAD_RESTORED, 23 VERIFY, 24 NO_TIME, 26 R2_TRANSFER, and a cancellation caused by the **job timeout** | 10 CONFIG, 11 R2_AUTH, 12 R2_MISSING, 13 ARCHIVE_INVALID, 14 DEPS, 15 BOT_START, 22 R2_UPLOAD_LOST, 25 CONFLICT, and a **manual Cancel** (130 with more than 180 s of job time left) |

A fresh runner always starts from what R2 holds, so restarting after a failed publish is safe; the
causes in the right-hand column would simply repeat, so they stop the chain.

### 7.3 If the previous run is still active

The workflow has `concurrency: {group: snowcell-rdp, cancel-in-progress: false}`. A new run **waits**; it is
never started in parallel and the running session is never cancelled. GitHub keeps at most one pending
run per group, so a second dispatch replaces the first pending one. Because the restart job checks for
queued and running runs first, a duplicate is not even created. A manual dispatch during a session
behaves the same way: it queues behind it. Two sets of bots can therefore never share the same Telegram
sessions or the same `snowcell.zip`.

### 7.4 Permissions

* **Built-in token (default).** GitHub's documentation lists `workflow_dispatch` as an exception to the rule
  that events created with `GITHUB_TOKEN` do not start new runs, so the token is enough. The job asks for
  `actions: write` and `contents: read` at job level; the workflow-wide default stays `contents: read`.
* **If your organisation caps `GITHUB_TOKEN` at read-only**, create a **fine-grained personal access token**
  (or a GitHub App installation token) limited to this one repository with **Actions: Read and write**, and
  store it as the secret `SNOWCELL_DISPATCH_TOKEN`. Both the restart job and the watchdog use it automatically
  when it exists. Never put it in a file.

### 7.5 The watchdog (optional)

`.github/workflows/watchdog.yml` runs at minutes 7 and 37 of every hour and has its own concurrency group.
It does **nothing** when any session is queued or running. When none is, it restarts the chain only if the
latest run ended `success` or `timed_out`, was `cancelled` at or after 350 minutes (a job timeout, not a
person), or `failed` after at least 30 minutes, and at least `SNOWCELL_WATCHDOG_GRACE_MINUTES` (10) have
passed. It never starts the very first session and obeys both kill switches. Delete the file if you do not
want it.

### 7.6 Limits you should know

* **Not 356 minutes.** The next run cannot start before the previous one has finished (that is the point of
  the concurrency group), and a new Windows runner needs a minute or two to be provisioned. The real
  cadence is the length of the job (up to 359 minutes) plus the hand-over, roughly 360 to 362 minutes.
  Between two sessions the RDP endpoint is down, and the Tailscale hostname and IP change; read them from
  the new run's summary.
* **The chain's weakest point** is what GitHub does after a *job timeout*: whether the final job still
  sees the earlier job's outputs and `!cancelled()` is true is not spelled out in the documentation. The
  watchdog exists for exactly this case; it steps in 10 to 40 minutes after the run ended (30-minute cron
  interval, 10-minute grace), plus any delay GitHub adds to scheduled runs.
* **Schedules are best-effort**: delayed or dropped under load, default branch only, and disabled by GitHub
  after 60 days without repository activity on public repositories.
* **To stop the chain**: set `SNOWCELL_AUTORESTART=false` **first**, then cancel the running session, then
  cancel any pending run. Cancelling first would let the final job or the watchdog queue another run.
* **Cost and terms.** On private repositories Windows minutes are billed at a higher rate than Linux
  minutes, and a chain of six-hour sessions consumes minutes around the clock. GitHub's terms for
  Actions on GitHub-hosted runners also limit use to activity related to producing, testing, deploying or
  publishing the repository's software project. A permanent RDP-and-bots host is outside that wording, so
  GitHub could throttle or suspend such use. That risk exists with a single session too; a continuous
  chain makes it larger.

---

## 8. The four-minute window: 355-minute session versus 359-minute job

### 8.1 The arithmetic

Times are minutes since the first workflow step ("Record job start time"). GitHub's own job clock started a few
seconds earlier, which is why `setup.py` keeps a 60-second margin.

```
 0              first step starts
 S              all five bots are running
                S = setup: secrets, checkout, Python, RDP, ATOMA, Tailscale and WARP (two WinGet
                installs and two connections), profile, app kick-off, R2 download, extraction,
                5-second start-up check
 S + 355        shared deadline: shutdown begins
 S + 355 + P    shutdown finished
                P = stop (up to 45 s grace) + settle + zip + hash + delete + upload + verify
 359            GitHub cancels the job (timeout-minutes: 359)
```

The shutdown finishes inside the job only if `S + 355 + P <= 359`, that is **`S + P <= 4 minutes`**.
The Tailscale and WARP installs alone take longer than that. The offline check shows the best case: with only the
60-second margin used and not one setup step run, it prints `Post-runtime window : 3.0 min`. With a realistic
`S` of 8 minutes the shutdown would begin at minute 363, four minutes *after* the limit.

**355 minutes of bot time plus setup plus a verified upload cannot fit in 359 minutes, and nothing in the
code can make it fit.** (360 is GitHub's maximum for a job on GitHub-hosted runners, so no setting of
`timeout-minutes` fixes it either.) The shipped default keeps both of your numbers exactly as written and does
not claim otherwise.

### 8.2 What happens in the default configuration

1. At minute 359 GitHub cancels the job: Ctrl+C to the running step, Ctrl+Break 7.5 seconds later, the process
   tree is killed 2.5 seconds after that.
2. `setup.py` stops the bots on the first signal (grace shortened to 6 seconds), records the time and exits
   with 130 without touching R2.
3. The `if: always()` recovery step rebuilds the archive, compares, replaces and verifies, inside the
   5-minute window that GitHub documents for cancellation.

**The risk is that GitHub ends the job before the upload happens.** The documentation describes the 5-minute
window for cancellation; it does not say that the same window applies after a job *timeout*. If the recovery step
does not run, or is cut off before the upload completes, this session's changes are lost. The previous
`snowcell.zip` stays in R2 in that case: the old object is deleted only after the replacement exists locally and
has been validated, and `setup.py` refuses to start the delete when the remaining time cannot cover the upload
(exit 24). The bots would also have run `359 - S` minutes, not 355 (351 minutes when `S` is 8).

### 8.3 What is already optimised (without weakening integrity or secret isolation)

* `pip install` runs **while** the archive downloads and extracts, not after.
* The checkout fetches only `scripts/`; `--check-config` fails a bad configuration in the first minute.
* The five bots start together; there is no sequential start-up.
* Hashing and zipping are single streaming passes; the verification uses `HEAD` plus R2's server-side
  SHA-256, and adds a full re-download only when the time budget allows it.
* The destructive step is never started unless the budget covers the upload (`NO_TIME`, exit 24).
* The grace period for a bot to stop is configurable (`SNOWCELL_SHUTDOWN_GRACE_SECONDS`, default 45).

Not done, because it would trade integrity or your requirements for minutes: skipping WARP or Tailscale,
skipping validation or the SHA-256 comparison, sharing secrets with the bots, a shorter safety reserve.

### 8.4 Options (your decision)

| | Option | What you change | Result | Cost |
|---|---|---|---|---|
| **A** | **Keep as shipped (current default)** | nothing | 355 minutes counted from the moment the bots run; limit 359 | GitHub cancels the job before the deadline whenever `S > 4`; saving relies on the recovery step after a timeout, which I could not verify |
| **B** | **Job-anchored deadline (my recommendation)** | repository variable `SNOWCELL_DEADLINE_MODE=job-anchored` | the bots stop 5 minutes before the job limit; stop, zip, hash, delete, upload and verify run on the normal path with a guaranteed 5-minute window | bot time is about `353 - S` minutes (345 when `S` is 8), never exactly 355 |
| **C** | Lower `session_minutes` | change `default: "355"` of the input, or type a value per run (also `SNOWCELL_SESSION_MINUTES` for the watchdog) | a fixed, predictable runtime such as 340 | you choose the number by hand and must keep `S + runtime + P <= 359` |
| **D** | Raise the job limit to 360 | every `359` in `setup.yml` (the `timeout-minutes` line and the three `SNOWCELL_JOB_TIMEOUT_MINUTES` lines) and in `watchdog.yml` (`JOB_TIMEOUT_MINUTES`) | one more minute | does not solve it; 360 is the platform maximum |
| **E** | Start the bots earlier (before Tailscale, WARP and the app installs) | re-order steps; the bot process must outlive its step | gains about `S - 2` minutes | changes your original step order and the step contract; not attempted |

Option **A** is what you asked for, and it is only as safe as the recovery path. Choose **A** only after the
timeout-path test in section 10.5 has passed on your repository. Option **B** needs no file edit and can be
undone by deleting the variable.

```powershell
gh variable set    SNOWCELL_DEADLINE_MODE --body job-anchored --repo OWNER/REPO   # option B
gh variable delete SNOWCELL_DEADLINE_MODE --repo OWNER/REPO                       # back to option A
```

### 8.5 Measure `S` and `P` on your own runner

Step 11 prints the budget twice. The first group, `Job-time budget`, is a forecast made before the download.
The second, **`Job-time budget (measured now that the bots are running)`**, is exact:

```
Elapsed before bots start  : 8.4 min      <- S
Planned bot runtime        : 355.0 min
Shutdown would begin at    : 363.4 min after job start
Post-runtime window        : -4.4 min     <- negative: GitHub cancels first
```

A GitHub warning annotation appears with it whenever the window is negative or shorter than 5 minutes.
`P` is visible after the run: the time between `Monitoring ended` and `Persistent data saved` in the same log.

---

## 9. Exit codes

The step fails (the job turns red) for every code other than 0. "Chain" says whether the auto-restart may queue a
successor (section 7.2).

| Code | Name | Meaning | R2 object afterwards | Chain |
|---|---|---|---|---|
| 0 | `OK` | Replaced and verified, or provably unchanged | current | yes |
| 1 | `UNEXPECTED` | Unhandled error; traceback with secrets masked is in the log | check `Phase` and `Publish decision` in the step summary | yes |
| 10 | `CONFIG` | Missing or invalid secret or variable, wrong bucket or key, endpoint and account id disagree, bucket not found, or an unfinished state file from an earlier run | untouched | no |
| 11 | `R2_AUTH` | R2 rejected the credentials (401/403, `InvalidAccessKeyId`, `SignatureDoesNotMatch`, `AccessDenied`) | untouched | no |
| 12 | `R2_MISSING` | `snowcell.zip` does not exist in the bucket | untouched | no |
| 13 | `ARCHIVE_INVALID` | Download is not a valid or safe archive, a bot folder is missing or already exists at the destination | untouched | no |
| 14 | `DEPS` | `pip install` failed or timed out, or boto3 is unavailable | untouched | no |
| 15 | `BOT_START` | No bot could be launched, all exited within 5 seconds, or at least one could not be launched (data is still saved) | saved if the run got that far | no |
| 16 | `BOT_CRASH` | At least one bot exited before the deadline (data is still saved) | current | yes |
| 17 | `SHUTDOWN` | A bot process could not be confirmed stopped; the archive was **not** rebuilt | untouched | yes |
| 18 | `ZIP_BUILD` | The rebuilt archive failed to build or validate | untouched | yes |
| 19 | `HASH` | A local archive could not be hashed, or changed on disk before the upload | untouched | yes |
| 20 | `R2_DELETE` | The old object could not be deleted | still the original | yes |
| 21 | `R2_UPLOAD_RESTORED` | The upload failed; the original was restored and verified | the original (this session's changes are not saved) | yes |
| 22 | `R2_UPLOAD_LOST` | The upload **and** the restore failed | **missing or unverified: act now (section 11.6)** | no |
| 23 | `VERIFY` | The stored object did not match (original restored), or R2 could not be reached during verification (replacement left in place) | see the log | yes |
| 24 | `NO_TIME` | Not enough job time to replace the object safely | untouched (this session's changes are not saved) | yes |
| 25 | `CONFLICT` | R2 changed while the bots ran; nothing was overwritten | not touched by this run | no |
| 26 | `R2_TRANSFER` | Network or transfer error talking to R2 | untouched | yes |
| 130 | `INTERRUPTED` | Stop requested by Cancel or the job timeout; archive and upload are left to the recovery step | decided by the recovery step | only after a timeout |

The step summary table (`Result`, `Phase`, `Publish decision`, both SHA-256 values, one row per bot) is the first place
to look. `Phase` is one of `init`, `downloaded`, `extracted`, `running`, `stopped`, `built`, `interrupted`,
`publish-failed`, `shutdown-failed`, `complete`.

---

## 10. Verification checklist

### 10.1 Before the first run

* [ ] Files are at the paths in section 1; `python -m unittest discover -s tests` passes.
* [ ] `gh secret list` shows the five R2 secrets plus `RDP_PASSWORD` and `TAILSCALE_AUTHKEY`; `gh variable list`
      shows `R2_BUCKET_NAME=snowcell` and `R2_OBJECT_KEY=snowcell.zip`.
* [ ] Bucket `snowcell` is private; the token is **Object Read & Write** on that bucket only.
* [ ] `aws s3api head-object` (section 3) returns the object and its size.
* [ ] An offline copy of `snowcell.zip` exists outside R2.
* [ ] You have chosen A, B or C in section 8.4.

### 10.2 In the Actions log (first run, `session_minutes` = 10)

* [ ] `🧪 Check Snow Cell configuration`: `R2 configuration is structurally valid`.
* [ ] Your original steps are green; `📁 Prepare ATOMA Downloads folder` prints `ATOMA Downloads folder: C:\Users\ATOMA\Downloads`.
* [ ] `Found snowcell/snowcell.zip (...)`, then `Downloaded ... SHA-256 <hash>`: write the hash down.
* [ ] `Archive OK: ... files`, `Extracted into C:\Users\ATOMA\Downloads\snowcell`.
* [ ] `Dependencies installed in ...s: Telethon==1.44.0, pytz, phonenumbers, aiofiles, aiosqlite, boto3`.
* [ ] `Bot environment: N credential-bearing variable(s) removed (...)` lists names such as `R2_ACCESS_KEY_ID`,
      `RDP_PASSWORD`, `TAILSCALE_AUTHKEY` and **no values**.
* [ ] Five `Launched <Bot> (pid ...)` lines, then `Startup check: 5/5 bots running`.
* [ ] `All bots launched. Shared deadline: ...` and the measured `Job-time budget` group (section 8.5).
* [ ] `Session active; N minutes remaining; bots running 5/5` once a minute.
* [ ] After the deadline: `Stopping 5 bot(s)`, five `stopped (graceful), exit code 0` lines, `All bot processes have stopped`.
* [ ] `SHA-256 of the downloaded archive` and `SHA-256 of the rebuilt archive`, then **either**
      `No changes detected` **or** `The archive bytes differ`, `Changes detected: ...`, `Deleting the old object`,
      `Uploading the replacement archive`, `Replacement archive uploaded to snowcell/snowcell.zip and verified (...)`.
* [ ] `Persistent data saved` and `Done: success.`; the step summary shows `OK` (exit code 0).
* [ ] The recovery step shows `Recovery: the main step finished; nothing to do.`
* [ ] Searching the raw log for your R2 Access Key ID finds nothing.

### 10.3 On the machine (RDP over Tailscale, using the address in the run summary)

* [ ] The desktop of `ATOMA` has shortcuts for Telegram Desktop, WhatsApp (or WhatsApp Web), Google Chrome, Tailscale
      and Cloudflare WARP, **without anyone having logged in before**.
* [ ] `C:\ProgramData\ATOMA-AppSetup\status.txt` reads `SUCCESS ...`; the log is `install.log` next to it.
* [ ] `C:\Users\ATOMA\Downloads\snowcell\<Bot>\bot.py` exists for all five bots, with `Data` and `Sessions`.
* [ ] `C:\Users\ATOMA\Downloads\snowcell-logs\<Bot>.log` exists; five `python.exe` processes are running.
* [ ] `C:\Users` contains `ATOMA` and **not** `ATOMA.<COMPUTERNAME>`.

### 10.4 In R2 after the run

* [ ] The bucket holds exactly one object, `snowcell.zip`; its modified time and size match
      `New archive validated: ...` in the log. No `snowcell-new.zip`, `-backup`, timestamped or temporary keys,
      and no unfinished multipart uploads.
* [ ] Compare hashes from your own computer with the `SHA-256 of the rebuilt archive` line:

```powershell
aws s3api get-object --bucket snowcell --key snowcell.zip `
  --endpoint-url https://<ACCOUNT_ID>.r2.cloudflarestorage.com snowcell-check.zip
Get-FileHash .\snowcell-check.zip -Algorithm SHA256
```

* [ ] The archive opens and contains the five bot folders with `bot.py`, `Data` and `Sessions`.
* [ ] The bucket is still private (no public access, no custom domain).

### 10.5 Failure paths (do these once; they use the real object, so keep the offline copy)

Set `SNOWCELL_AUTORESTART=false` for these tests so that no chain starts.

* **Cancel path.** Start a run with `session_minutes` = 15, wait for the first `Session active` line, press
  *Cancel run*. Expect `Monitoring ended (interrupted)`, `All bot processes have stopped`, then in the recovery step
  `Recovery: the main step ended in phase 'interrupted'` and `Recovery complete: persistent data saved.`
* **Timeout path (this settles option A).** On a throw-away branch set `timeout-minutes: 12` and
  `SNOWCELL_JOB_TIMEOUT_MINUTES: "12"` (check, run and recover steps), run with `session_minutes` = 60. At minute 12 GitHub
  cancels the job. If the recovery step then runs and ends with `Recovery complete`, the timeout path works on your
  repository. If it does not run, use option B or C.
* **Restart chain.** Set `SNOWCELL_MIN_RESTART_MINUTES=5`, remove `SNOWCELL_AUTORESTART`, run with `session_minutes` = 10. When
  the run ends, `♻️ Queue the next session` dispatches a new run that waits and starts only after the first has
  completed. Then set `SNOWCELL_AUTORESTART=false`, cancel the new run, and restore the minimum to 30.
* **Overlap.** Dispatch the workflow twice in a row. The second run stays *pending*; two sessions are never active together.
* The R2 failure paths (refused upload, lost response, missing object, unreachable R2) are covered by the offline
  tests; do not provoke them on the real bucket.

---

## 11. Troubleshooting

### 11.1 Where to look first

1. The **step summary** of the run: `Result`, `Phase`, `Publish decision`, both SHA-256 values, one row per bot.
2. The log of step 11 (collapsed groups: configuration, budget, download, extract, rebuild, compare).
3. Step 12 (the recovery step): did it run, and what did it say?
4. On the machine (RDP): `C:\Users\ATOMA\Downloads\snowcell-logs\<Bot>.log` (each bot's output),
   `%RUNNER_TEMP%\snowcell-work\` (`state.json`, `original.zip`, `new.zip`, manifests),
   `C:\ProgramData\ATOMA-AppSetup\` (`install.log`, `status.txt`).

### 11.2 Configuration and credentials (exit 10, 11)

| Message | Cause and fix |
|---|---|
| `Missing required secrets/variables: R2_... Create them under Settings > Secrets and variables > Actions.` | Add the names it lists. `R2_BUCKET_NAME` and `R2_OBJECT_KEY` are **variables**; the other four R2 values are **secrets**. |
| `R2_BUCKET_NAME must be exactly 'snowcell'.` / `R2_OBJECT_KEY must be exactly 'snowcell.zip'; alternate final keys are refused.` | Intended guard. Correct the variable. |
| `R2_ENDPOINT host must look like <account-id>[.<jurisdiction>].r2.cloudflarestorage.com` | Wrong host. Use the bare S3 endpoint from section 3. |
| `R2_ENDPOINT must be the bare S3 API endpoint (no bucket name or path).` / `... must start with https://` / `... must not use a custom port.` | Remove the path, bucket name or port. |
| `R2_ACCOUNT_ID does not match the account id inside R2_ENDPOINT.` | The two secrets belong to different accounts; fix one. |
| `Bucket not found. Check R2_BUCKET_NAME and that R2_ENDPOINT carries the bucket's jurisdiction` | The bucket is in a jurisdiction (EU, FedRAMP, US) and the endpoint has none, or the reverse. |
| `R2 rejected the request (AccessDenied / 403 / InvalidAccessKeyId / SignatureDoesNotMatch)` | Wrong pair, deleted or expired token (check its TTL), token not **Object Read & Write**, token not scoped to `snowcell`, or a client-IP restriction on the token (runner addresses change every run). Rejected credentials are never retried. |
| `<NAME> must be an integer ...` / `<NAME> must be one of: ...` | An optional variable has an invalid value (for example `job_anchored` with an underscore). An empty value means "use the default". |
| `A previous run left an unfinished state. Run 'python scripts/setup.py --recover' first ...` | Only possible when the work folder survives (self-hosted runner). Run `--recover`, or delete `snowcell-work`. |

### 11.3 Download, archive, dependencies (exit 12, 13, 14, 26)

| Message | Cause and fix |
|---|---|
| `snowcell.zip does not exist in bucket snowcell. Upload your initial snowcell.zip once` | First run, or the object was deleted. Upload your offline copy (section 3). |
| `Download: network/transfer error ... Check R2_ENDPOINT, DNS and the WARP/Tailscale state.` | Transient network trouble (WARP and Tailscale both alter routing). Four attempts are made. Re-run; check the `WARP state` line of the earlier step. |
| `incomplete download (X of Y bytes)` | Transient; retried. |
| `Archive is missing <Bot>/bot.py` | The ZIP layout is wrong (section 1). List it with `python -m zipfile -l snowcell.zip`. |
| `Path traversal in archive` / `Unsafe absolute path` / `Drive letter / stream marker` / `Archive contains a symbolic link` / `Duplicate or case-colliding entry` / `Reserved Windows device name` / `Illegal character` / `Archive contains encrypted entries` | The archive is refused as unsafe before anything is written. Rebuild it with a plain `zip` of the five folders. |
| `Archive would unpack to ..., above the configured limit.` | Default limit is 8 GiB. After checking the archive is genuine, raise `SNOWCELL_MAX_UNPACKED_MIB` in the step `env:`. |
| `<folder> already contains <Bot>...; refusing to overwrite.` | The destination is not empty (self-hosted runner, or `SNOWCELL_BOT_ROOT` points at an existing install). Remove it or choose another folder. |
| `pip install failed with exit code N:` with the last lines of pip | No network (WARP), a PyPI outage, or a package that cannot be installed on Python 3.12. The command is exactly `python -m pip install "Telethon==1.44.0" pytz phonenumbers aiofiles aiosqlite boto3`; run it by hand over RDP to see the full error. A 15-minute limit applies. |

### 11.4 Bots (exit 15, 16, 17)

* **`Startup check: 0/5 bots running`**, **`All bots exited during startup`**, **`<Bot> exited unexpectedly with code N after M min`**:
  read the `Last N log lines of <Bot>` group (or `<Bot>.log` over RDP). Usual causes: a dependency beyond the pinned list,
  a revoked or invalid file in `Sessions`, an import or syntax error, or a bot expecting an environment variable
  that is deliberately not passed (section 6).
* The default policy `continue` keeps the other four running when one crashes. `SNOWCELL_FAILURE_POLICY=abort`
  ends the session (and saves the data) on the first crash. If all five exit early the session ends early, the data is
  saved and the run reports exit 16. The RDP session ends with it, which is intended: a session without bots is
  replaced by a fresh one through the auto-restart chain (when the run lasted at least `SNOWCELL_MIN_RESTART_MINUTES`),
  so the bots are back within minutes instead of staying down until minute 355.
* **`<Bot> ignored the graceful stop for 45s; terminating it.`**: the bot's own shutdown takes longer than the grace period,
  or it does not handle `KeyboardInterrupt`. A forced stop risks losing the last SQLite transaction. Raise
  `SNOWCELL_SHUTDOWN_GRACE_SECONDS` (maximum 600) only if the post-run window (section 8) can afford it.
* A Windows exit code of `3221225786 (0xC000013A)` means the process ended through Ctrl+C or Ctrl+Break without
  clean-up. It is expected only when you set `SNOWCELL_BOT_LAUNCH=direct`; with the default launcher a clean stop is exit 0.
* **`Could not confirm that every bot stopped: ... The archive was NOT rebuilt and the R2 object was NOT touched.`** (exit 17):
  some process whose command line mentions the bot folder survived termination. Nothing was changed in R2; the next
  session starts from the previous object.

### 11.5 Timeouts and cancellations (exit 24, 130)

* **The run ended at minute 359 and the recovery step says `Recovery complete`**: option A worked as designed (section 8).
* **`Only Ns of job time remain but about Ms are needed to replace the object safely.`** (exit 24): the session's changes were
  not saved and R2 is untouched. Choose option B or C, and keep the archive small (old logs inside `Data` are the usual cause).
* **The recovery step did not run, or ended before uploading**: the assumption behind option A failed. Use B or C.
* **Cancelled by hand**: the bots are stopped and the recovery step saves the data; no successor is queued.

### 11.6 Replace, upload and restore (exit 19 to 26)

The step summary's `Publish decision` tells you what happened:

| `Publish decision` | Exit | Meaning and next step |
|---|---|---|
| `replaced` | 0 | Replaced and verified. |
| `unchanged-bytes` / `unchanged-content` | 0 | Nothing to upload; R2 untouched. |
| `skipped-no-time` | 24 | Not enough job time; R2 untouched (section 11.5). |
| `delete-failed` | 20 | The old object is still there. Check that the token includes delete (Object Read & Write) and Cloudflare's status page. |
| `upload-failed-original-restored` | 21 | The upload failed, the original was put back and verified. Read `Replacement upload FAILED: ...` for the cause (403 on write: token permission; network; a very large archive). |
| `verify-failed-original-restored` | 23 | The stored object did not match what was sent; the original was restored. Re-run, and watch for transfer problems. |
| `verify-unreachable` | 23 | The replacement was uploaded but R2 could not be asked to confirm it. Compare its SHA-256 with the logged value yourself (section 10.4). |
| `conflict` | 25 | The object in R2 changed while the bots ran (a manual upload, another tool). **Nothing was overwritten**, and this session's changes exist only on the runner. Never edit `snowcell.zip` while a session is running. |
| `upload-failed-restore-failed`, `verify-failed-restore-failed` | 22 | **Critical**: R2 may hold no object. See below. |

**Exit 22: `CRITICAL: the replacement upload AND the restore failed; the R2 object may be missing.`**

1. Open the recovery step of the **same run**. It runs after the failed step, sees that the object is missing and tries
   the upload of the rebuilt archive once more. If it ends with `Replacement archive uploaded ... and verified`, you are done.
2. If the object is still missing, upload your offline copy of `snowcell.zip` to the exact key `snowcell.zip`
   (dashboard, Objects, Upload). It holds the state from before the lost session; that session's changes are gone, because the
   runner is destroyed when the job ends.
3. Fix the cause (token permission, outage, network) before you start another session. A new run against a missing
   object stops at once with exit 12 and changes nothing.

### 11.7 RDP, Tailscale, WARP and the apps

* `Tailscale authentication or connection failed.` / `Could not obtain the Tailscale IPv4 address.`: the auth key is
  expired, revoked or single-use. Create a new key and update `TAILSCALE_AUTHKEY`.
* `RDP is not reachable through Tailscale.`: the check in your original step; look at the Tailscale and firewall steps above it.
* `WARP state`: `CONNECTED`, `DISCONNECTED_TO_PRESERVE_RDP` (WARP broke RDP and was switched off, as your step intends) or
  `MANUAL_SETUP_REQUIRED` (activation failed).
* `The ATOMA profile was not created within 2 minutes.` (a warning): the Downloads folder was created without a registered
  profile, so Windows may create `C:\Users\ATOMA.<COMPUTERNAME>` at the first RDP login. Check the background task error
  printed above the warning.
* App status values in `status.txt`: `PENDING`, `RUNNING`, `SUCCESS`, `SUCCESS (with notes)` (for example the WhatsApp Web shortcut) and
  `FAILED: ...`. The full transcript is `install.log`. `Could not start the background installation` means the first-logon task
  will do the work at the first RDP login instead.

### 11.8 Changing the bot code or the archive by hand

A session replaces `snowcell.zip` at its end, and a manual upload during a session is detected as a conflict (exit 25).
To update the bots: stop the chain (section 7.6), wait for the running session to finish, upload the new `snowcell.zip`,
re-enable `SNOWCELL_AUTORESTART`, and start the workflow once by hand.

---

## 12. Review of your original workflow

I read all 657 lines of your current file before changing anything. Findings, with what was done:

| # | Finding in the original | What I did |
|---|---|---|
| 1 | **Timing.** The keep-alive loop counts its `session_minutes` from the start of its own step, after all setup steps, while the job limit is 360. With 355 minutes that leaves 5 minutes for every earlier step, so the limit can end the job before the loop does (the "Session duration reached" line is never printed). Harmless while nothing had to be saved at the end. | `timeout-minutes` set to 359 as you required; the consequence for saving is section 8. |
| 2 | **App installation waits for a login.** The task has an `AtLogOn` trigger with an interactive principal, so nothing installs until someone signs in over RDP. | Kept as a fallback; a second task started immediately with ATOMA's password performs a background logon and installs Telegram, WhatsApp and Chrome without any login. |
| 3 | **WhatsApp through the `msstore` source.** Windows Server has no Microsoft Store, so this install is expected to fail. A failed package marks the whole setup `FAILED` and `success.flag` is never written, so the task would re-run and fail at every login. | Telegram falls back to the official installer; WhatsApp falls back to a WhatsApp Web app shortcut; only a real miss counts as a failure. Could not be run here. |
| 4 | **No profile before the first logon.** `New-LocalUser` does not create `C:\Users\ATOMA`; a folder created by hand is not a registered profile, and Windows then makes `ATOMA.<COMPUTERNAME>` at the first login. | A password-logon task creates the real profile and `Downloads` first (step 8b). |
| 5 | **No concurrency control.** Two dispatches would run two machines with the same Telegram sessions and the same `snowcell.zip`. Telegram can invalidate a session that is used from two places at once. | Workflow-level `concurrency` with `cancel-in-progress: false`. |
| 6 | **Auth key on a command line.** `tailscale up "--authkey=..."` is masked in the log but visible in the process list while it runs. | Left unchanged (you asked to preserve it); the documented `--auth-key=file:<path>` form is the later fix. |
| 7 | **Job-level `env` exposes `RDP_PASSWORD` and `TAILSCALE_AUTHKEY` to every step.** | Kept, because your steps need them; the bots receive neither (section 6). |
| 8 | **Display step** told people to log in as ATOMA. | Two text lines changed; the bots folder is shown. Nothing else in that step changed. |

Checked and left alone because no fault was found: the NLA/TLS registry values, the firewall rule scoped to
`100.64.0.0/10` with the built-in Remote Desktop rules disabled, the account and group handling, the Tailscale
connection and IP discovery, the WARP activation with its disconnect-to-preserve-RDP fallback, and the step summary.

**Rules to keep when you edit the inner PowerShell scripts.** They are written by `pwsh` as BOM-less UTF-8 and read by
Windows PowerShell 5.1, which treats such a file as ANSI: keep them **pure ASCII** (a test enforces this), build them as an
array of lines (no here-strings inside the YAML), and remember that 5.1 turns native-command stderr into terminating errors
under `$ErrorActionPreference = "Stop"`.

---

## 13. Limitations and unverified items

* **Never executed on a real runner.** The PowerShell steps (background logon tasks, app install, WhatsApp fallback) are
  structure-linted only; the Windows branches of `setup.py` (`CTRL_BREAK_EVENT`, `taskkill`, the process scan) are tested with mocks.
* **After a job timeout**, whether the recovery step runs and whether the final job sees the earlier job's outputs is not
  stated in GitHub's documentation (the cancellation page does not mention `timeout-minutes`). Section 10.5 tests it.
* **Real R2**: the `ChecksumSHA256` upload header, `HEAD` with `ChecksumMode`, and that **Object Read & Write** includes
  delete are expected from Cloudflare's documentation but not exercised here. Fallbacks exist for the first two.
* **Delete first, then upload** (your specified order) leaves a window of seconds in which R2 holds no object. A hard loss of the
  runner inside that window (VM failure) would leave it missing. A plain `PutObject` to the same key replaces an object in
  one step and has no such window; it was not used because your specification says delete, then upload. Say so if you
  want that variant. The offline copy is the protection either way.
* **Large archives**: above about 4.5 GiB the upload is multipart, which has no whole-object checksum; verification then
  relies on size and the full re-download.
* **Graceful stop depends on the bots** handling `KeyboardInterrupt`. Otherwise they are terminated after the grace period.
* **Environment isolation, not a sandbox** (section 6).
* **Cost and terms** (section 7.6): minutes are billed, and GitHub's terms restrict what GitHub-hosted runners may be used for.
* All run state lives on the runner and disappears when the job ends; only the R2 object persists.

---

## 14. Sources

GitHub
* [Workflow cancellation](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-cancellation): sequence (SIGINT, 7500 ms, SIGTERM, 2500 ms, kill), `if` re-evaluation, 5-minute limit.
* [Triggering a workflow](https://docs.github.com/actions/using-workflows/triggering-a-workflow): events created with `GITHUB_TOKEN`, and the `workflow_dispatch` exception.
* [Concurrency](https://docs.github.com/en/actions/concepts/workflows-and-actions/concurrency) and the [2026-05-07 changelog](https://github.blog/changelog/2026-05-07-github-actions-concurrency-groups-now-allow-larger-queues): one running and one pending run per group, a newer pending run replaces the older, `queue: max` is opt-in.
* [GitHub Terms for Additional Products and Features](https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features): Actions usage restrictions.
* [actions/checkout](https://github.com/actions/checkout) and [actions/setup-python](https://github.com/actions/setup-python): both used at `v7` (setup-python 7 removed only the `pip-install` input, which this workflow does not use).

Cloudflare and Tailscale
* [R2 data location and jurisdictions](https://developers.cloudflare.com/r2/reference/data-location/): jurisdiction endpoints (`eu`, `fedramp`, `us`).
* [R2 with boto3](https://developers.cloudflare.com/r2/examples/boto3) and [R2 with the AWS CLI](https://developers.cloudflare.com/r2/examples/aws/aws-cli/): endpoint format and region `auto`.
* [`tailscale up`](https://tailscale.com/docs/reference/tailscale-cli/up): `--auth-key=file:<path>`.
