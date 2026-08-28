"""Deterministic Playwright boundary for real web-UI product tests.

The adapter is optional and fail-closed.  It never falls back to curl, an HTTP
status probe, a mock DOM, or an LLM interpretation.  A browser PASS requires a
positive interaction and a positive semantic UI assertion.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import re
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

from .product_testing import _execution_environment, validate_product_command
from .security import redact_text

BROWSER_ADAPTER_SCHEMA = "quality-pilot.browser-adapter.v1"
BROWSER_ADAPTER_VERSION = "1.0.0"


def run_browser_test(
    settings: Mapping[str, Any],
    *,
    cwd: Path,
    evidence_dir: Path,
    contract_identity_hash: str,
    environment_profile: Mapping[str, Any] | None,
    timeout_ms: int = 60_000,
    dry_run: bool = False,
    root: Path | None = None,
    playwright_python: str | Path | None = None,
    case_id: str | None = None,
    run_id: str | None = None,
    prestarted: bool = False,
    server_url: str | None = None,
    server_stdout_path: Path | None = None,
    server_stderr_path: Path | None = None,
    server_command: str | None = None,
) -> dict[str, Any]:
    """Run one bounded, real Playwright interaction flow."""
    root = root or cwd
    case_id = case_id or "BROWSER-UI"

    def case_result(result: dict[str, Any]) -> dict[str, Any]:
        result.update({"case_id": case_id, "run_id": run_id})
        return result
    if not bool(settings.get("enabled")):
        result = _result("NOT_RUN", "browser_ui_not_enabled", contract_identity_hash)
        result.update({"case_id": case_id, "run_id": run_id})
        return result
    url = str(server_url or settings.get("url") or settings.get("base_url") or "").strip()
    start_command = str(server_command or settings.get("start_command") or "").strip()
    url_discovery = str(settings.get("url_discovery") or "").strip().lower()
    url_pattern = str(settings.get("url_pattern") or r"https?://[^\s]+")
    steps = settings.get("steps") if isinstance(settings.get("steps"), list) else []
    if (not url and url_discovery != "stdout") or (not prestarted and not start_command) or not steps:
        result = _result("BLOCK", "browser_contract_missing", contract_identity_hash)
        result.update({"case_id": case_id, "run_id": run_id})
        return result
    parsed_url = urlsplit(url) if url else None
    if parsed_url and (parsed_url.scheme not in {"http", "https"} or parsed_url.hostname not in {"localhost", "127.0.0.1", "::1"}):
        return case_result(_result("BLOCK", "browser_remote_url_not_supported", contract_identity_hash))
    safety = {"status": "SAFE", "argv": []} if prestarted else validate_product_command(start_command, cwd=cwd)
    if safety.get("status") != "SAFE":
        return case_result(_result("BLOCK", f"unsafe_browser_start_command:{safety.get('reason')}", contract_identity_hash))
    if dry_run:
        return case_result({
            **_result("PLANNED", "dry_run", contract_identity_hash),
            "url": _redact_url(url),
            "step_count": len(steps),
        })
    if environment_profile is not None and not bool(environment_profile.get("ready")):
        return case_result(_result("BLOCK", "environment_profile_required", contract_identity_hash))

    try:
        _add_playwright_site_packages(playwright_python, cwd=cwd)
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError:
        return case_result(_result("BLOCK", "browser_prerequisites_absent", contract_identity_hash, missing=["python_playwright"]))

    evidence_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = evidence_dir / "server.stdout.log"
    stderr_path = evidence_dir / "server.stderr.log"
    source_stdout_path = server_stdout_path if prestarted and server_stdout_path is not None else stdout_path
    source_stderr_path = server_stderr_path if prestarted and server_stderr_path is not None else stderr_path
    screenshot_path = evidence_dir / "screenshot.png"
    trace_path = evidence_dir / "trace.zip"
    interaction_path = evidence_dir / "interaction.json"
    server_meta_path = evidence_dir / "server.meta.json"
    process: subprocess.Popen[bytes] | None = None
    browser = None
    context = None
    page = None
    interactions: list[dict[str, Any]] = []
    positive_assertions = 0
    state_assertions = 0
    interaction_count = 0
    status = "BLOCK"
    reason = "browser_launch_failed"
    failure_type = None
    error_detail: str | None = None
    current_step: dict[str, Any] | None = None
    console_events: list[dict[str, Any]] = []
    network_events: list[dict[str, Any]] = []
    dom_path = evidence_dir / "dom.html"
    diagnostics_path = evidence_dir / "diagnostics.json"
    started = time.monotonic()
    try:
        if not prestarted:
            stdout_handle = stdout_path.open("wb")
            stderr_handle = stderr_path.open("wb")
            try:
                process = subprocess.Popen(
                    safety["argv"],
                    cwd=cwd,
                    env=_execution_environment(environment_profile),
                    shell=False,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    start_new_session=True,
                )
            finally:
                stdout_handle.close()
                stderr_handle.close()
        server_meta_path.write_text(
            json.dumps(
                {
                    "schema": "quality-pilot.browser-server.v1",
                    "command": start_command,
                    "argv": safety["argv"],
                    "url": _redact_url(url),
                    "timeout_ms": timeout_ms,
                    "prestarted": prestarted,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        @contextmanager
        def playwright_with_failure_capture():
            # Playwright's context manager stops its event loop before an
            # outer exception handler runs. Capture failure artifacts inside
            # the live Playwright boundary so screenshots, DOM snapshots, and
            # traces remain available for diagnosis.
            with sync_playwright() as playwright:
                try:
                    yield playwright
                except Exception:
                    _capture_browser_artifacts(page, screenshot_path, context, trace_path)
                    try:
                        dom_text = page.content() if page is not None else ""
                        for value in _url_sensitive_values(url):
                            dom_text = dom_text.replace(value, "[REDACTED:runtime_token]")
                        dom_text, _ = redact_text(dom_text, path=str(dom_path))
                        dom_path.write_text(dom_text, encoding="utf-8")
                    except Exception:
                        pass
                    raise

        with playwright_with_failure_capture() as playwright:
            browser_type = getattr(playwright, str(settings.get("browser") or "chromium"), None)
            if browser_type is None:
                return case_result(_result("BLOCK", "unsupported_browser", contract_identity_hash))
            executable = str(browser_type.executable_path)
            if not executable or not Path(executable).exists():
                return case_result(_result("BLOCK", "browser_prerequisites_absent", contract_identity_hash, missing=["browser_binary"]))
            browser = browser_type.launch(headless=True)
            context = browser.new_context(
                viewport={
                    "width": int(settings.get("width") or 1200),
                    "height": int(settings.get("height") or 800),
                }
            )
            context.tracing.start(screenshots=True, snapshots=True, sources=False)
            page = context.new_page()
            page.on("console", lambda message: console_events.append({"type": str(getattr(message, "type", "")), "text": _redact_url(str(getattr(message, "text", "")))}))
            page.on("request", lambda request: network_events.append({"kind": "request", "url": _redact_url(str(getattr(request, "url", ""))), "method": str(getattr(request, "method", ""))}))
            page.on("response", lambda response: network_events.append({"kind": "response", "url": _redact_url(str(getattr(response, "url", ""))), "status": int(getattr(response, "status", 0) or 0)}))
            if not url and url_discovery == "stdout":
                deadline = time.monotonic() + max(1, timeout_ms) / 1000
                discovered = None
                while time.monotonic() < deadline:
                    try:
                        output = source_stdout_path.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        output = ""
                    match = re.search(url_pattern, output)
                    if match:
                        discovered = _extract_discovered_url(match.group(0))
                        break
                    if process is not None and process.poll() is not None:
                        break
                    time.sleep(0.05)
                if not discovered:
                    raise RuntimeError("browser_url_discovery_failed")
                url = discovered
                parsed_url = urlsplit(url)
                server_meta_path.write_text(json.dumps({"schema": "quality-pilot.browser-server.v1", "command": start_command, "url": _redact_url(url), "url_discovery": "stdout", "token_redacted": True, "prestarted": prestarted}, indent=2) + "\n", encoding="utf-8")
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            for index, step in enumerate(steps):
                if not isinstance(step, Mapping):
                    raise ValueError("browser_step_invalid")
                action = str(step.get("action") or "").strip().lower()
                selector = str(step.get("selector") or "").strip()
                current_step = {"index": index, "action": action, "selector": selector}
                timeout = int(step.get("timeout_ms") or timeout_ms)
                item: dict[str, Any] = {"index": index, "action": action, "selector": selector}
                if action == "goto":
                    page.goto(str(step.get("url") or url), wait_until="domcontentloaded", timeout=timeout)
                elif action == "click":
                    locator = page.locator(selector)
                    try:
                        locator.click(timeout=timeout)
                    except PlaywrightTimeoutError:
                        # Some Chromium/container combinations report a
                        # permanently unstable layout even for a static
                        # visible button. A force click is acceptable only
                        # after deterministic locator, visibility, enabled,
                        # and overlay checks; it is still a real Playwright
                        # interaction and is recorded explicitly.
                        if not _safe_force_click(page, selector):
                            raise
                        locator.click(timeout=min(timeout, 1_000), force=True)
                        item["interaction_mode"] = "force_after_stability_timeout"
                    interaction_count += 1
                elif action == "fill":
                    page.locator(selector).fill(_resolve_value(str(step.get("value") or "")), timeout=timeout)
                    interaction_count += 1
                elif action == "press":
                    page.locator(selector).press(str(step.get("key") or "Enter"), timeout=timeout)
                    interaction_count += 1
                elif action == "select_option":
                    page.locator(selector).select_option(str(step.get("value") or ""), timeout=timeout)
                    interaction_count += 1
                elif action == "expect_visible":
                    locator = page.locator(selector)
                    count = locator.count()
                    if count == 0:
                        # Preserve Playwright's normal timeout/oracle
                        # classification for a selector that is absent.
                        locator.wait_for(state="visible", timeout=timeout)
                    else:
                        visible_count = sum(1 for item_index in range(count) if locator.nth(item_index).is_visible())
                        if visible_count == 0:
                            raise AssertionError(f"browser_selector_not_visible:{selector}")
                        item["matching_count"] = count
                        item["visible_count"] = visible_count
                    positive_assertions += 1
                elif action == "expect_text":
                    page.locator(selector).wait_for(state="visible", timeout=timeout)
                    actual = page.locator(selector).inner_text(timeout=timeout)
                    expected = str(step.get("expected") or "")
                    if expected not in actual:
                        raise AssertionError(f"browser_text_mismatch:{selector}")
                    positive_assertions += 1
                    state_assertions += 1
                elif action == "expect_url":
                    expected = str(step.get("expected") or "")
                    if expected not in page.url:
                        raise AssertionError("browser_url_mismatch")
                    positive_assertions += 1
                    state_assertions += 1
                else:
                    raise ValueError(f"browser_action_unsupported:{action}")
                item["status"] = "PASS"
                interactions.append(item)
            page.screenshot(path=str(screenshot_path), full_page=True)
            context.tracing.stop(path=str(trace_path))
            browser.close()
            browser = None
        if interaction_count < 1 or state_assertions < 1:
            status, reason = "HOLD", "browser_probe_only_no_semantic_state_assertion"
        else:
            status, reason = "PASS", "browser_semantic_interaction_passed"
    except AssertionError as exc:
        status, reason, failure_type = "FAIL", str(exc), "PRODUCT_UI_FAILURE"
        _capture_browser_artifacts(page, screenshot_path, context, trace_path)
    except (PlaywrightTimeoutError, TimeoutError) as exc:
        failure_type = _classify_timeout(page, current_step)
        status = "FAIL" if failure_type == "PRODUCT_UI_FAILURE" else ("BLOCK" if failure_type == "HARNESS_INTERACTION_FAILURE" else "HOLD")
        reason = f"browser_interaction_timeout:{failure_type}"
        _capture_browser_artifacts(page, screenshot_path, context, trace_path)
    except PlaywrightError as exc:
        failure_type = "BROWSER_STARTUP_BLOCK" if page is None else "HARNESS_INTERACTION_FAILURE"
        status = "BLOCK"
        reason = "browser_runtime_failed"
        error_detail, _ = redact_text(str(exc), path="browser.playwright.error")
        _capture_browser_artifacts(page, screenshot_path, context, trace_path)
    except (OSError, ValueError, RuntimeError) as exc:
        failure_type = "BROWSER_STARTUP_BLOCK" if page is None else "TIMEOUT_UNCLASSIFIED"
        status = "BLOCK" if failure_type == "BROWSER_STARTUP_BLOCK" else "HOLD"
        reason = str(exc)
        _capture_browser_artifacts(page, screenshot_path, context, trace_path)
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        _terminate_process(process)

    diagnostics = _collect_browser_diagnostics(page, current_step, console_events, network_events)
    diagnostics_path.write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if page is not None:
        try:
            dom_text = page.content()
            for value in _url_sensitive_values(url):
                dom_text = dom_text.replace(value, "[REDACTED:runtime_token]")
            dom_text, _ = redact_text(dom_text, path=str(dom_path))
            dom_path.write_text(dom_text, encoding="utf-8")
        except Exception:
            pass
    stdout = _redact_file(source_stdout_path, sensitive_values=_url_sensitive_values(url))
    stderr = _redact_file(source_stderr_path, sensitive_values=_url_sensitive_values(url))
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    if trace_path.exists():
        _sanitize_trace(trace_path, _url_sensitive_values(url))
    interaction_path.write_text(json.dumps(interactions, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result_details = {"error": error_detail} if error_detail else {}
    result = case_result(_result(status, reason, contract_identity_hash, **result_details))
    result.update(
        {
            "url": _redact_url(url),
            "browser": str(settings.get("browser") or "chromium"),
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "interaction_count": interaction_count,
            "positive_assertion_count": positive_assertions,
            "state_assertion_count": state_assertions,
            "failure_type": failure_type,
            "evidence": {
                "server_stdout": _relative_or_str(stdout_path, root),
                "server_stderr": _relative_or_str(stderr_path, root),
                "server_meta": _relative_or_str(server_meta_path, root),
                "interaction": _relative_or_str(interaction_path, root),
                "screenshot": _relative_or_str(screenshot_path, root) if screenshot_path.exists() else None,
                "screenshot_sha256": _sha256_file(screenshot_path) if screenshot_path.exists() else None,
                "trace": _relative_or_str(trace_path, root) if trace_path.exists() else None,
                "dom": _relative_or_str(dom_path, root) if dom_path.exists() else None,
                "diagnostics": _relative_or_str(diagnostics_path, root) if diagnostics_path.exists() else None,
                "console": _relative_or_str(diagnostics_path, root) if diagnostics_path.exists() else None,
                "network": _relative_or_str(diagnostics_path, root) if diagnostics_path.exists() else None,
            },
        }
    )
    return result


def _safe_force_click(page: Any, selector: str) -> bool:
    try:
        locator = page.locator(selector)
        if locator.count() != 1 or not locator.is_visible() or not locator.is_enabled() or locator.bounding_box() is None:
            return False
        overlay = page.evaluate(
            """(selector) => { const el = document.querySelector(selector); if (!el) return null; const r = el.getBoundingClientRect(); const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2); return top && top !== el && !el.contains(top) ? {tag: top.tagName, id: top.id || '', className: top.className || ''} : null; }""",
            selector,
        )
        return not overlay
    except Exception:
        return False


def _classify_timeout(page: Any, step: Mapping[str, Any] | None) -> str:
    if page is None or not isinstance(step, Mapping) or not str(step.get("selector") or ""):
        return "ORACLE_MISSING"
    selector = str(step.get("selector") or "")
    try:
        locator = page.locator(selector)
        if locator.count() == 0:
            return "ORACLE_MISSING"
        if not locator.is_visible() or not locator.is_enabled():
            return "PRODUCT_UI_FAILURE"
        overlay = page.evaluate(
            """(selector) => { const el = document.querySelector(selector); if (!el) return null; const r = el.getBoundingClientRect(); const top = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2); return top && top !== el && !el.contains(top) ? {tag: top.tagName, id: top.id || '', className: top.className || ''} : null; }""",
            selector,
        )
        if overlay:
            return "HARNESS_INTERACTION_FAILURE"
        return "TIMEOUT_UNCLASSIFIED"
    except Exception:
        return "TIMEOUT_UNCLASSIFIED"


def _collect_browser_diagnostics(
    page: Any,
    step: Mapping[str, Any] | None,
    console_events: list[dict[str, Any]],
    network_events: list[dict[str, Any]],
) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {
        "schema": "quality-pilot.browser-diagnostics.v1",
        "step": dict(step or {}),
        "console": list(console_events)[-200:],
        "network": list(network_events)[-200:],
        "page_url": None,
        "viewport": None,
        "selector": None,
    }
    if page is None:
        return diagnostics
    diagnostics["page_url"] = _redact_url(str(getattr(page, "url", "") or ""))
    try:
        diagnostics["viewport"] = page.viewport_size
    except Exception:
        pass
    selector = str((step or {}).get("selector") or "")
    diagnostics["selector"] = selector or None
    if selector:
        try:
            locator = page.locator(selector)
            count = locator.count()
            visible_count = sum(1 for item_index in range(count) if locator.nth(item_index).is_visible())
            diagnostics["locator"] = {
                "count": count,
                "visible": visible_count > 0,
                "visible_count": visible_count,
                "enabled": locator.nth(0).is_enabled() if count else False,
                "aria_disabled": locator.nth(0).get_attribute("aria-disabled") if count else None,
            }
        except Exception:
            diagnostics["locator"] = {"status": "unavailable"}
    return diagnostics


def _capture_browser_artifacts(page: Any, screenshot: Path, context: Any, trace: Path) -> None:
    if page is not None:
        try:
            page.screenshot(path=str(screenshot), full_page=True)
        except Exception:
            pass
    if context is not None:
        try:
            context.tracing.stop(path=str(trace))
        except Exception:
            pass


def _add_playwright_site_packages(
    python_executable: str | Path | None,
    *,
    cwd: Path,
) -> None:
    """Make the selected review venv's Python packages available to this process.

    The Hermes/Quality Pilot dispatcher may itself run under system Python,
    while the disposable review venv owns Playwright.  Browser execution is
    still local to this process and uses the selected venv's package set; no
    pip install is attempted here and no product/remote interpreter is used.
    """
    if not python_executable:
        return
    candidate = Path(str(python_executable))
    if not candidate.is_absolute():
        candidate = cwd / candidate
    if candidate.name not in {"python", "python3"} or candidate.parent.name != "bin":
        return
    venv_root = candidate.parent.parent
    site_roots = sorted(venv_root.glob("lib/python*/site-packages"))
    for site_root in reversed(site_roots):
        if site_root.is_dir() and str(site_root) not in sys.path:
            sys.path.insert(0, str(site_root))


def _terminate_process(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except Exception:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except Exception:
            process.kill()


def _resolve_value(value: str) -> str:
    value = str(value)
    if value.startswith("${") and value.endswith("}"):
        return os.environ.get(value[2:-1], "")
    if value.startswith("$"):
        return os.environ.get(value[1:], "")
    return value


def _redact_file(path: Path, *, sensitive_values: list[str] | None = None) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for value in sensitive_values or []:
        if value:
            text = text.replace(value, "[REDACTED:runtime_token]")
    redacted, _ = redact_text(text, path=str(path))
    return redacted


def _extract_discovered_url(value: str) -> str:
    candidate = str(value or "").strip()
    nested = re.search(r"https?://[^\s]+", candidate)
    if nested:
        candidate = nested.group(0)
    return candidate.rstrip(".,)\"'")


def _redact_url(value: str) -> str:
    parsed = urlsplit(str(value or ""))
    if not parsed.query:
        return str(value or "")
    query_parts: list[str] = []
    for item in parsed.query.split("&"):
        key, separator, _raw = item.partition("=")
        if not separator:
            query_parts.append(key)
        else:
            query_parts.append(f"{key}=<redacted>")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "&".join(query_parts), ""))


def _url_sensitive_values(value: str) -> list[str]:
    parsed = urlsplit(str(value or ""))
    values: list[str] = []
    for item in parsed.query.split("&"):
        key, separator, raw = item.partition("=")
        if separator and raw and key.lower() in {"token", "authtoken", "auth", "key", "secret", "password", "access_token", "accesstoken", "session", "jwt"}:
            values.append(raw)
    return values


def _sanitize_trace(path: Path, sensitive_values: list[str]) -> None:
    """Rewrite text entries in a Playwright trace before it becomes evidence."""
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="quality-pilot-trace-", suffix=".zip", delete=False, dir=str(path.parent)) as handle:
            temporary = Path(handle.name)
        with zipfile.ZipFile(path, "r") as source, zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as target:
            for info in source.infolist():
                data = source.read(info.filename)
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    target.writestr(info, data)
                    continue
                for value in sensitive_values:
                    if value:
                        text = text.replace(value, "[REDACTED:runtime_token]")
                redacted, _ = redact_text(text, path=f"trace:{info.filename}")
                target.writestr(info, redacted.encode("utf-8"))
        temporary.replace(path)
    except (OSError, zipfile.BadZipFile):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _result(status: str, reason: str, contract_identity_hash: str, **details: Any) -> dict[str, Any]:
    result = {
        "case_type": "playwright_ui",
        "schema": BROWSER_ADAPTER_SCHEMA,
        "adapter_version": BROWSER_ADAPTER_VERSION,
        "status": status,
        "reason": reason,
        "contract_identity_hash": contract_identity_hash,
        "evidence": {},
    }
    result.update(details)
    return result


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_or_str(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path.resolve())
