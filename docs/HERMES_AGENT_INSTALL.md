# Hermes Agent Installation

This guide is written for the Hermes Agent installed on this machine.

Important correction: Hermes does not load QA-AIST by reading `qa-aist.agent.json` from `/usr/local/lib/hermes-agent/agent`. Hermes dynamic slash commands are generated from skills under:

```text
~/.hermes/skills/**/SKILL.md
```

So the practical way to make `/qa-aist ...` visible in Hermes is to install QA-AIST as a Hermes skill.

## What This Gives You

After installation, Hermes will recognize:

```text
/qa-aist status
/qa-aist doctor
/qa-aist qa-test list
/qa-aist close-loop run-once
```

Boundary: this is a Hermes skill slash command. Hermes converts `/qa-aist ...` into a skill invocation message, and the skill instructs the agent to call QA-AIST's deterministic dispatcher. This is not the same as a native Hermes Python router directly executing QA-AIST before the LLM sees the turn.

## Install Without System pip

On Ubuntu, system Python may reject `pip install .` with `externally-managed-environment`. You can still install the Hermes skill directly from the QA-AIST checkout.

From the QA-AIST repo:

```bash
cd /root/repo/QA-AIST
PYTHONPATH=/root/repo/QA-AIST/src python3 -m qa_aist.hermes install-skill --force \
  --runner-command "/usr/bin/env PYTHONPATH=/root/repo/QA-AIST/src python3 -m qa_aist.hermes"
```

This creates:

```text
/root/.hermes/skills/qa-aist/SKILL.md
```

Check it:

```bash
PYTHONPATH=/root/repo/QA-AIST/src python3 -m qa_aist.hermes skill-status
```

Expected:

```json
{
  "status": "ok",
  "command_prefix": "/qa-aist",
  "skill_valid": true
}
```

Then reload skills inside Hermes:

```text
/reload-skills
```

Now try:

```text
/qa-aist doctor
```

## Verify Hermes Can See It

From a terminal, you can ask Hermes' own skill scanner whether `/qa-aist` exists:

```bash
PYTHONPATH=/usr/local/lib/hermes-agent python3 - <<'PY'
from agent.skill_commands import scan_skill_commands
cmds = scan_skill_commands()
print("/qa-aist" in cmds)
print(cmds.get("/qa-aist"))
PY
```

Expected first line:

```text
True
```

## Verify QA-AIST Dispatcher Directly

Before blaming Hermes, check QA-AIST itself:

```bash
cd /path/to/product-repo
PYTHONPATH=/root/repo/QA-AIST/src python3 -m qa_aist.hermes --root "$PWD" /qa-aist doctor
```

If the product repo has not been initialized yet, run:

```bash
PYTHONPATH=/root/repo/QA-AIST/src python3 -m qa_aist.hermes --root "$PWD" /qa-aist setup
PYTHONPATH=/root/repo/QA-AIST/src python3 -m qa_aist.hermes --root "$PWD" /qa-aist doctor
```

## If You Want Real Console Scripts

The shorter command:

```bash
qa-aist-hermes install-skill
```

works only after QA-AIST is installed into the Python environment Hermes uses.

If `qa-aist-hermes: command not found`, either use the `PYTHONPATH=... python3 -m qa_aist.hermes ...` form above, or install into a venv/pipx environment and ensure Hermes can see that command.

## Why `/usr/local/lib/hermes-agent/agent` Was Wrong

`/usr/local/lib/hermes-agent/agent` contains Hermes Agent Python source files. Dropping `qa-aist.agent.json` into that directory does not register a slash command.

Hermes dynamic slash commands come from:

```text
~/.hermes/skills/<skill-name>/SKILL.md
```

The command name is derived from the skill frontmatter:

```yaml
---
name: qa-aist
description: ...
---
```

That is why QA-AIST now provides:

```bash
python3 -m qa_aist.hermes install-skill
```

## If You Need Native Deterministic Routing

The skill route still depends on the Hermes agent following the skill instructions and using the terminal tool. For a fully native deterministic route, Hermes itself must add a slash command handler that calls:

```python
from qa_aist.hermes import dispatch_chat_command

result = dispatch_chat_command("/qa-aist doctor", root=session.project_root)
return result["chat_response"]
```

That requires a Hermes code/plugin change. The skill installation is the working no-Hermes-code-change path.
