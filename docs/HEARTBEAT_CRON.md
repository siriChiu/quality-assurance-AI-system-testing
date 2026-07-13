# Heartbeat and external scheduling

Status: **Supported as a single tick; scheduling is external.**

`/quality-pilot close-loop heartbeat` does not create a daemon, cron entry, or
Hermes timer. One invocation performs one sensor-driven tick, persists the
result, and exits. The default 12-hour value is scheduling metadata, not proof
that a future invocation has been installed.

## Before scheduling

Run these from the target product repository, not the AI Quality Pilot source
checkout:

```bash
quality-pilot doctor --root /srv/your-product
quality-pilot audit state --root /srv/your-product
quality-pilot close-loop heartbeat --root /srv/your-product --dry-run
quality-pilot close-loop heartbeat --root /srv/your-product
```

Confirm all of the following:

- `/srv/your-product` is the intended product root.
- The runtime entry point and side-effect boundary are correct.
- Required fixture paths and credential **environment variable names** are configured.
- The scheduler process can see the same product binary and non-secret environment as the verified manual run.
- External-resource tests and remote writes remain gated; a timer must not be used to bypass confirmation.
- `.quality-pilot-project/state/close-loop/heartbeat-latest.json` records the expected tick.

## Hermes scheduler

If the Hermes deployment provides a heartbeat or recurring-task facility,
configure it to invoke exactly one dispatcher tick in the target product
workspace:

```text
/quality-pilot close-loop heartbeat
```

The Hermes task must retain the target repository context. It should surface
`needs_input`, `BLOCKED`, failed gates, and remote-write requests to a human
instead of repeatedly retrying them as if they were new local work. The exact
timer installation mechanism belongs to the Hermes deployment; AI Quality
Pilot only owns the tick contract.

## Cron example

The following example runs every 12 hours. Replace both absolute paths. Run a
manual tick successfully before installing it.

```cron
17 */12 * * * cd /srv/your-product && /usr/bin/flock -n .quality-pilot-project/state/close-loop/heartbeat.lock /home/qa/.local/bin/quality-pilot close-loop heartbeat --root /srv/your-product --fail-on-test-failure >> .quality-pilot-project/state/close-loop/heartbeat-cron.log 2>&1
```

Why the example is deliberately explicit:

- `cd` and `--root` prevent state from being written into the wrong repository.
- Absolute executable paths avoid cron's restricted `PATH` surprises.
- `flock -n` prevents overlapping ticks; a busy tick is skipped instead of duplicated.
- `--fail-on-test-failure` gives the scheduler a non-zero exit when QA fails.
- Output stays in the target overlay, alongside heartbeat state.

Do not put raw secrets in the crontab. Provide credentials through the
deployment's protected environment mechanism and store only their environment
variable names in `.quality-pilot.yaml`.

## Operations and recovery

After each scheduled tick, monitor:

```text
.quality-pilot-project/state/close-loop/heartbeat-latest.json
.quality-pilot-project/state/close-loop/heartbeat-history.jsonl
.quality-pilot-project/state/close-loop/heartbeat-cron.log
```

Expected no-work behavior is `idle`; it is not a failure. Use the persisted
`qa_outcome` and `alert_required` fields, plus the process exit status, for
monitoring. If a tick is blocked,
run `quality-pilot doctor`, `quality-pilot audit state`, and `quality-pilot
close-loop status` interactively in the same product root. Resolve the reported
missing fact or gate, then invoke a new tick. Do not delete state merely to force
the loop forward.

To disable cron, remove the cron entry. Removing the timer does not delete cases,
evidence, reports, or heartbeat history.
