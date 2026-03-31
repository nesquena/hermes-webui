     1|# Portability Plan for Hermes Web UI MVP
     2|
     3|This document describes what would need to change for this repository to be **download-and-run friendly** for the widest possible set of Hermes agent installations.
     4|
     5|Assumption: the user already has a working Hermes agent somewhere, either:
     6|- on their local machine, or
     7|- on a VPS they can SSH into.
     8|
     9|Goal: a new user should be able to clone this repo and have it discover their existing Hermes installation with as little manual setup as possible.
    10|
    11|---
    12|
    13|## What should be true in the portable version
    14|
    15|A portable version should be able to:
    16|
    17|1. Find the Hermes agent automatically, or via one small override.
    18|2. Find the Python environment automatically, or use a user-supplied override.
    19|3. Pick a sane state directory without hard-coded user paths.
    20|4. Run locally or against a remote VPS with minimal configuration.
    21|5. Avoid requiring repo-specific paths like `~/...`.
    22|6. Avoid assuming the repo lives inside a specific parent checkout.
    23|7. Avoid assuming the user has the same workspace naming, session layout, or directory structure.
    24|8. Keep tests isolated regardless of the user’s setup.
    25|9. Document the bootstrap flow clearly enough that a first-time user can succeed.
    26|
    27|---
    28|
    29|## Current portability blockers
    30|
    31|### 1. Hard-coded Hermes agent paths
    32|Current code and scripts assume things like:
    33|- `<agent-dir>/venv/bin/python`
    34|- `<agent-dir>`
    35|- `<repo>/server.py`
    36|
    37|These paths make the repo work for one specific machine layout, but not for other Hermes installs.
    38|
    39|### 2. Hard-coded state locations
    40|The repo currently expects state under something like:
    41|- `~/.hermes/webui-mvp/`
    42|- `~/.hermes/webui-mvp-test/`
    43|
    44|That is reasonable as a default, but it should be configurable and auto-discoverable.
    45|
    46|### 3. The start script is environment-specific
    47|`start.sh` currently assumes:
    48|- a specific Python venv path
    49|- a specific agent checkout path
    50|- a specific working directory
    51|
    52|### 4. Tests assume a fixed install shape
    53|The test fixture currently assumes:
    54|- the Hermes agent repo exists at a known path
    55|- the Python interpreter is in a known venv location
    56|- the state directory lives under `~/.hermes`
    57|
    58|### 5. Documentation reveals internal paths
    59|The docs currently embed:
    60|- absolute local paths
    61|- a specific VPS IP example
    62|- tunnel commands tailored to the current setup
    63|
    64|That is fine for internal use, but a portable public version should replace those with discovery-based or example-only instructions.
    65|
    66|---
    67|
    68|## The portability changes that would be needed
    69|
    70|### A. Add a real bootstrap/discovery layer
    71|
    72|Create a small bootstrap routine whose job is to determine:
    73|- where Hermes agent code lives,
    74|- what Python executable should be used,
    75|- what state directory should be used,
    76|- whether the UI is running locally or remotely.
    77|
    78|Suggested discovery order:
    79|
    80|1. Explicit environment variables.
    81|2. A nearby sibling Hermes checkout.
    82|3. A parent directory that looks like the Hermes agent repo.
    83|4. Common default paths.
    84|5. A helpful error if nothing matches.
    85|
    86|Suggested environment variables:
    87|- `HERMES_WEBUI_AGENT_DIR`
    88|- `HERMES_WEBUI_PYTHON`
    89|- `HERMES_WEBUI_STATE_DIR`
    90|- `HERMES_WEBUI_DEFAULT_WORKSPACE`
    91|- `HERMES_WEBUI_HOST`
    92|- `HERMES_WEBUI_PORT`
    93|- `HERMES_CONFIG_PATH`
    94|- `HERMES_HOME`
    95|
    96|What this buys you:
    97|- fewer instructions for the user,
    98|- fewer assumptions in code,
    99|- easier support for local and VPS installs.
   100|
   101|---
   102|
   103|### B. Make the start script generic
   104|
   105|`start.sh` should:
   106|
   107|1. Discover the repo root dynamically.
   108|2. Discover the Python executable from one of:
   109|   - `HERMES_WEBUI_PYTHON`
   110|   - a local `.venv/bin/python`
   111|   - the Hermes agent venv if found nearby
   112|   - `python3` as a fallback if appropriate
   113|3. Discover the Hermes agent directory from:
   114|   - `HERMES_WEBUI_AGENT_DIR`
   115|   - a sibling checkout
   116|   - a parent checkout
   117|4. Avoid hard-coded absolute paths.
   118|5. Print the chosen paths before starting, so the user can see what was detected.
   119|6. Fail with a clear message if Hermes cannot be found.
   120|
   121|Optional nice-to-have:
   122|- a `--dry-run` or `--print-config` mode that shows what would be used.
   123|
   124|---
   125|
   126|### C. Separate “repo location” from “Hermes agent location”
   127|
   128|The repo should not care where it itself lives.
   129|It should only care about:
   130|- the repo root for its own code,
   131|- the Hermes agent root for imports and runtime integration.
   132|
   133|This means the code should stop assuming things like:
   134|- this repo lives under `~`,
   135|- the agent repo is exactly one directory up or down from here.
   136|
   137|Instead, resolve paths based on:
   138|- runtime config,
   139|- env vars,
   140|- actual filesystem discovery.
   141|
   142|---
   143|
   144|### D. Add a dedicated config resolution module
   145|
   146|Create one place in the code that resolves all runtime configuration.
   147|That module should handle:
   148|- host and port,
   149|- state directory,
   150|- agent directory,
   151|- default workspace,
   152|- model,
   153|- config file loading,
   154|- any future public/private toggles.
   155|
   156|Why this matters:
   157|- fewer scattered `Path.home()` and `os.getenv()` calls,
   158|- easier support for Linux, macOS, containers, and VPS installs,
   159|- one clear place to document defaults.
   160|
   161|---
   162|
   163|### E. Make Hermes agent import discovery robust
   164|
   165|Right now the server injects a parent directory into `sys.path` so Hermes modules can be imported.
   166|That should become more flexible.
   167|
   168|Suggested improvements:
   169|- try an explicit Hermes agent path first,
   170|- support a user-provided `PYTHONPATH` or equivalent override,
   171|- check for the required Hermes modules before startup,
   172|- show a clear error if the expected modules are unavailable.
   173|
   174|This is important because different users may have Hermes arranged as:
   175|- a monorepo,
   176|- separate sibling checkouts,
   177|- a virtualenv-only install,
   178|- a deployed VPS layout.
   179|
   180|---
   181|
   182|### F. Make state storage portable and isolated
   183|
   184|The UI should continue keeping state outside the repo, but the location should be configurable.
   185|
   186|State should include:
   187|- sessions
   188|- workspaces
   189|- last workspace
   190|- cron/job state
   191|- skills/memory references, if relevant
   192|- any UI-specific caches
   193|
   194|Recommended behavior:
   195|1. Default to a stable user-scoped directory.
   196|2. Allow override via `HERMES_WEBUI_STATE_DIR` or `HERMES_HOME`.
   197|3. Create missing directories automatically.
   198|4. Never write state into the source tree unless explicitly requested.
   199|5. Keep test state isolated from production state.
   200|
   201|---
   202|
   203|### G. Make workspace selection auto-detectable
   204|
   205|The UI should not require the user to manually point at a workspace every time.
   206|
   207|Good portable behavior:
   208|- restore the last workspace if it still exists,
   209|- fall back to a user-configurable default workspace,
   210|- fall back to the agent’s standard workspace if one exists,
   211|- if nothing is known, prompt once and remember the selection.
   212|
   213|Possible discovery sources:
   214|- `HERMES_WEBUI_DEFAULT_WORKSPACE`
   215|- `HERMES_HOME`
   216|- a workspace list file
   217|- a sibling workspace directory if present
   218|
   219|For VPS setups, the UI should work whether the workspace is:
   220|- inside the Hermes agent repo,
   221|- in a separate workspace directory,
   222|- or mounted from another location.
   223|
   224|---
   225|
   226|### H. Support both local and remote access patterns
   227|
   228|The portable version should work in either of these situations:
   229|
   230|1. Local Hermes agent, browser on same machine.
   231|2. Hermes running on a VPS, browser connected over SSH tunnel or reverse proxy.
   232|
   233|To support both:
   234|- keep binding configurable,
   235|- keep the default bind address safe (`127.0.0.1`),
   236|- document how to override it when the user intentionally wants remote binding,
   237|- avoid assuming a specific SSH hostname or IP.
   238|
   239|The docs should show examples like:
   240|- local: `http://127.0.0.1:<port>`
   241|- remote tunnel: `ssh -N -L <localport>:127.0.0.1:<remoteport> user@host`
   242|
   243|but never hard-code one user’s VPS details.
   244|
   245|---
   246|
   247|### I. Make tests path-independent
   248|
   249|The test suite should not rely on the current user’s machine layout.
   250|
   251|Tests should:
   252|- discover the repo root dynamically,
   253|- discover the Hermes agent location dynamically,
   254|- use temporary isolated state directories,
   255|- avoid touching real sessions or real cron jobs,
   256|- work on macOS, Linux, and VPS hosts where possible.
   257|
   258|Recommended test changes:
   259|1. Replace fixed `~/.hermes/hermes-agent` assumptions with environment-based discovery.
   260|2. Replace fixed `~/webui-mvp` assumptions with repo-relative discovery.
   261|3. Use temporary directories or test-specific state roots.
   262|4. Make the test server startup verify the discovered Hermes runtime before proceeding.
   263|5. Keep production state fully untouched.
   264|
   265|---
   266|
   267|### J. Add a first-run setup flow
   268|
   269|A public, portable release should include a minimal first-run flow.
   270|
   271|Best-case flow:
   272|1. User clones the repo.
   273|2. User runs `./start.sh` or `python server.py`.
   274|3. The app auto-detects Hermes.
   275|4. If detection succeeds, it starts.
   276|5. If detection fails, it prints one short fix-it block.
   277|
   278|If auto-detection fails, the app should ask for only the missing pieces, ideally one at a time.
   279|
   280|Examples:
   281|- “I found Python, but not the Hermes agent directory.”
   282|- “I found the agent, but not a valid virtualenv.”
   283|- “I found Hermes state, but no default workspace.”
   284|
   285|The goal is to avoid making users edit config files unless absolutely necessary.
   286|
   287|---
   288|
   289|### K. Make the public README setup-oriented, not machine-oriented
   290|
   291|The README should explain:
   292|- what Hermes Web UI expects from an existing Hermes install,
   293|- how auto-detection works,
   294|- what the fallback env vars are,
   295|- how to override detection only if needed,
   296|- how to run locally or through a VPS tunnel.
   297|
   298|It should not be written as if everyone has the exact same `~` layout.
   299|
   300|---
   301|
   302|### L. Clean up docs and examples for public release
   303|
   304|The following should be sanitized or made generic:
   305|- local file paths,
   306|- specific VPS IPs,
   307|- workspace names that reveal internal structure,
   308|- tunnel instructions that reference one machine,
   309|- any file names that imply private content or historical internal testing.
   310|
   311|Replace them with placeholders like:
   312|- `<HERMES_AGENT_DIR>`
   313|- `<STATE_DIR>`
   314|- `<SERVER_HOST>`
   315|- `<SERVER_PORT>`
   316|- `<WORKSPACE_PATH>`
   317|
   318|---
   319|
   320|## Recommended portability architecture
   321|
   322|The simplest portable architecture would be:
   323|
   324|### 1. One detection module
   325|A single module that resolves:
   326|- repo root,
   327|- agent root,
   328|- python executable,
   329|- state directory,
   330|- default workspace,
   331|- host and port.
   332|
   333|### 2. One configuration contract
   334|Environment variables plus a small optional config file.
   335|
   336|### 3. One startup path
   337|`start.sh` should call the same resolution logic as `server.py` so the CLI path and the runtime path match.
   338|
   339|### 4. One test isolation story
   340|Tests should use their own discovered state root and not care where Hermes was installed.
   341|
   342|### 5. One public-facing bootstrap doc
   343|The README should explain the portable flow in terms of discovery and overrides, not in terms of your machine.
   344|
   345|---
   346|
   347|## Suggested user experience after portability work
   348|
   349|A user should be able to do something like:
   350|
   351|```bash
   352|git clone <repo>
   353|cd <repo>
   354|./start.sh
   355|```
   356|
   357|and, if their Hermes setup is already valid, it should just work.
   358|
   359|If their setup differs, they should only need to provide one or two overrides, for example:
   360|
   361|```bash
   362|HERMES_WEBUI_AGENT_DIR=/path/to/hermes-agent \
   363|HERMES_WEBUI_PYTHON=/path/to/python \
   364|./start.sh
   365|```
   366|
   367|That is the target experience.
   368|
   369|---
   370|
   371|## Practical order of implementation
   372|
   373|If we were actually making this portable, I would do it in this order:
   374|
   375|1. Remove hard-coded paths from `start.sh`.
   376|2. Add config discovery in `api/config.py` or a new module.
   377|3. Make agent import resolution dynamic.
   378|4. Make tests use repo-relative and env-driven discovery.
   379|5. Replace public docs with generic instructions.
   380|6. Add a clear startup error path for missing Hermes components.
   381|7. Add a short “first run” section to README.
   382|8. Optionally add a `--print-config` mode for troubleshooting.
   383|
   384|---
   385|
   386|## Summary
   387|
   388|To make this repo truly portable, the main work is not feature work, it is **bootstrapping and discovery**.
   389|
   390|The repo currently works best when:
   391|- Hermes is already installed,
   392|- the directory layout matches your machine,
   393|- and the user is comfortable with some manual path knowledge.
   394|
   395|To make it work for the widest variety of Hermes setups, we need to:
   396|- remove hard-coded paths,
   397|- centralize config discovery,
   398|- make state locations configurable,
   399|- make tests isolated and path-independent,
   400|- and rewrite the onboarding docs around auto-detection.
   401|
   402|That would turn it from “works in my Hermes environment” into “clone it and it mostly figures itself out.”
   403|