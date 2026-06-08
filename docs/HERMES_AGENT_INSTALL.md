# Hermes Agent Installation

This guide explains how to make `/qa-aist ...` work inside a Hermes chat window.

Short version: QA-AIST cannot create a Hermes slash command by itself. Hermes must have a message router, plugin registry, agent registry, or tool configuration that maps `/qa-aist` to QA-AIST.

## What Must Be True

For a user to type this in Hermes:

```text
/qa-aist doctor
```

Hermes must do this internally:

```text
chat message "/qa-aist doctor"
  -> Hermes detects prefix "/qa-aist"
  -> Hermes calls QA-AIST adapter
  -> QA-AIST runs qa-aist engine with the current project root
  -> Hermes displays result.chat_response
```

QA-AIST provides the adapter. Hermes still needs to register it.

## Step 1: Install QA-AIST Where Hermes Can Run It

Install QA-AIST into the same Python environment that Hermes uses, or into a Python environment that Hermes can execute.

From a local checkout:

```bash
cd /path/to/qa-aist
python3 -m pip install .
```

Verify the package tools exist:

```bash
qa-aist --help
qa-aist-hermes --help
```

If Hermes runs inside a venv, activate that venv before installing:

```bash
source /path/to/hermes/.venv/bin/activate
cd /path/to/qa-aist
python3 -m pip install .
```

## Step 2: Verify QA-AIST Without Hermes

Use any product repo as the target root:

```bash
qa-aist-hermes --root /path/to/product-repo /qa-aist setup
qa-aist-hermes --root /path/to/product-repo /qa-aist doctor
```

Expected behavior:

- The command exits `0`.
- Output is JSON.
- The JSON contains `interface: hermes`.
- The JSON contains `chat_response`.

If this does not work, fix QA-AIST installation before touching Hermes.

## Step 3: Pick Your Hermes Integration Mode

Different Hermes installations expose different plugin surfaces. Pick the mode your Hermes actually supports.

| Hermes supports | Use this mode | What to register |
|---|---|---|
| External command/tool process | Process wrapper | `qa-aist-agent.sh` |
| Python plugin/callable | Python API | `qa_aist.hermes.dispatch_chat_command` |
| Manifest/agent directory | Portable manifest | `qa-aist.agent.json` |
| No plugin/tool/router support | Not directly possible | Use terminal/CI fallback or modify Hermes router |

## Mode A: External Process Wrapper

Generate the wrapper and manifest:

```bash
qa-aist-hermes install --agent-dir /path/to/hermes/agents/qa-aist
```

This creates:

```text
/path/to/hermes/agents/qa-aist/
  qa-aist.agent.json
  qa-aist-agent.sh
```

Register this in Hermes:

```yaml
name: qa-aist
trigger: /qa-aist
command: /path/to/hermes/agents/qa-aist/qa-aist-agent.sh
env:
  HERMES_PROJECT_ROOT: <current workspace root>
message:
  pass_as: argv
```

The YAML above is intentionally generic. Translate it to your Hermes config format.

Smoke test the wrapper:

```bash
HERMES_PROJECT_ROOT=/path/to/product-repo \
  /path/to/hermes/agents/qa-aist/qa-aist-agent.sh /qa-aist doctor
```

If Hermes passes the message through an environment variable instead of argv:

```bash
HERMES_PROJECT_ROOT=/path/to/product-repo \
HERMES_MESSAGE="/qa-aist doctor" \
  /path/to/hermes/agents/qa-aist/qa-aist-agent.sh
```

## Mode B: Python Plugin

If Hermes can import Python callables, register this callable:

```python
from qa_aist.hermes import dispatch_chat_command

def handle_message(message, session):
    if not message.startswith("/qa-aist "):
        return None

    result = dispatch_chat_command(
        message,
        root=session.project_root,
    )
    return result["chat_response"]
```

Hermes must provide:

- `message`: the full chat text, for example `/qa-aist doctor`.
- `session.project_root`: the current product repo root.

## Mode C: Manifest / Agent Directory

If Hermes can load a manifest, generate one:

```bash
qa-aist-hermes manifest
```

Or install files into an agent directory:

```bash
qa-aist-hermes install --agent-dir /path/to/hermes/agents/qa-aist
```

Then point Hermes at:

```text
/path/to/hermes/agents/qa-aist/qa-aist.agent.json
```

Important: `qa-aist.agent.json` is a portable manifest, not proof that your Hermes already understands this exact schema. If Hermes has its own schema, map these fields:

| QA-AIST manifest field | Hermes meaning |
|---|---|
| `command_prefix` | Chat trigger `/qa-aist` |
| `entrypoint.command` | Process command or wrapper |
| `entrypoint.root_env` | Env var for current project root |
| `entrypoint.message_env` | Env var for full chat message |
| `python_api` | Python callable mode |
| `permissions` | Filesystem/network/tracker permissions |
| `outputs.chat_response_field` | Text to display in chat |

## If Hermes Has No Plugin System

Then `/qa-aist ...` cannot work as a native chat command yet.

You still have two fallback options:

1. Use Hermes to run an external command manually:

```bash
qa-aist-hermes --root /path/to/product-repo /qa-aist doctor
```

2. Add a tiny router to Hermes:

```python
from qa_aist.hermes import dispatch_chat_command

def on_chat_message(message, session):
    if message.startswith("/qa-aist "):
        result = dispatch_chat_command(message, root=session.project_root)
        return result["chat_response"]
    return normal_hermes_flow(message, session)
```

## Final Verification

After registration, test in this order:

```text
/qa-aist status
/qa-aist doctor
/qa-aist qa-test list
/qa-aist qa-test run-one EXAMPLE-001
/qa-aist close-loop run-once
```

If `/qa-aist` is not recognized by Hermes, the failure is in Hermes registration, not in QA-AIST engine.

If Hermes recognizes `/qa-aist` but QA-AIST returns JSON with `status: error`, inspect `payload.error`.

If the command works outside Hermes but not inside Hermes, check:

- Hermes is using the same Python environment where QA-AIST is installed.
- Hermes passes the current product repo root.
- Hermes passes the full message, including `/qa-aist`.
- Hermes displays `chat_response` from the returned JSON.
