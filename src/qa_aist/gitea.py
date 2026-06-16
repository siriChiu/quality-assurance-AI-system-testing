from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib import parse, request


class GiteaError(RuntimeError):
    pass


@dataclass(frozen=True)
class GiteaConfig:
    backend: str
    base_url: str
    repo: str
    token_env: str
    token: str | None
    mcp_issues_json: str
    wiki_page: str
    branch_prefix: str

    @property
    def configured(self) -> bool:
        if self.uses_mcp:
            return bool(self.mcp_issues_json)
        return bool(self.base_url and self.repo and self.token)

    @property
    def uses_mcp(self) -> bool:
        return self.backend in {"mcp", "gitea-mcp", "hermes-mcp"}

    @property
    def uses_http(self) -> bool:
        return self.backend in {"", "http", "https", "rest", "api"}


def gitea_config_from_project(config_data: dict[str, Any]) -> GiteaConfig:
    tracker = config_data.get("tracker") if isinstance(config_data.get("tracker"), dict) else {}
    gitea = tracker.get("gitea") if isinstance(tracker.get("gitea"), dict) else {}
    mcp = tracker.get("mcp") if isinstance(tracker.get("mcp"), dict) else {}
    provider = str(tracker.get("provider") or "").strip().lower()
    backend = str(gitea.get("backend") or tracker.get("backend") or ("mcp" if provider == "hermes_mcp" or mcp else "http")).strip().lower()
    base_url = str(gitea.get("base_url") or tracker.get("base_url") or "").rstrip("/")
    repo = str(gitea.get("repo") or mcp.get("gitea_repo") or tracker.get("project") or tracker.get("repo") or "").strip("/")
    token_env = str(gitea.get("token_env") or tracker.get("api_token_env") or "")
    return GiteaConfig(
        backend=backend,
        base_url=base_url,
        repo=repo,
        token_env=token_env,
        token=(os.getenv(token_env) if token_env else None) or None,
        mcp_issues_json=str(gitea.get("mcp_issues_json") or mcp.get("gitea_issues_json") or tracker.get("mcp_issues_json") or ".qa-aist-project/state/gitea-mcp/issues.json"),
        wiki_page=str(gitea.get("wiki_page") or tracker.get("wiki_page") or mcp.get("wiki_page") or "Test status (Siri)"),
        branch_prefix=str(gitea.get("branch_prefix") or "qa-aist/issue-"),
    )


class GiteaClient:
    def __init__(self, config: GiteaConfig) -> None:
        if config.uses_mcp:
            raise GiteaError("Gitea MCP backend is read through the configured MCP issues JSON snapshot, not the HTTP client")
        if not config.base_url:
            raise GiteaError("gitea.base_url is required")
        if not config.repo:
            raise GiteaError("gitea.repo is required")
        if not config.token:
            raise GiteaError(f"Gitea token env is not set: {config.token_env}")
        self.config = config

    def list_issues(self, *, state: str = "all", include_comments: bool = True) -> list[dict[str, Any]]:
        items = self._request_json("GET", f"/repos/{self._repo_path()}/issues", query={"state": state, "type": "issues"})
        if not isinstance(items, list):
            raise GiteaError("Gitea issues response must be a list")
        issues = [item for item in items if isinstance(item, dict) and not item.get("pull_request")]
        if include_comments:
            for issue in issues:
                number = issue_number(issue)
                if number is not None:
                    issue["comments"] = self.list_issue_comments(number)
        return issues

    def list_issue_comments(self, issue_id: int) -> list[dict[str, Any]]:
        items = self._request_json("GET", f"/repos/{self._repo_path()}/issues/{issue_id}/comments")
        return items if isinstance(items, list) else []

    def create_issue(self, *, title: str, body: str, labels: list[int] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        created = self._request_json("POST", f"/repos/{self._repo_path()}/issues", payload=payload)
        if not isinstance(created, dict):
            raise GiteaError("Gitea create issue response must be an object")
        return created

    def create_issue_comment(self, issue_id: int, body: str) -> dict[str, Any]:
        created = self._request_json("POST", f"/repos/{self._repo_path()}/issues/{issue_id}/comments", payload={"body": body})
        if not isinstance(created, dict):
            raise GiteaError("Gitea create comment response must be an object")
        return created

    def update_wiki_page(self, *, page: str, content: str, message: str) -> dict[str, Any]:
        page_name = parse.quote(page, safe="")
        payload = {"content_base64": _to_base64_text(content), "message": message}
        updated = self._request_json("PATCH", f"/repos/{self._repo_path()}/wiki/page/{page_name}", payload=payload)
        if not isinstance(updated, dict):
            raise GiteaError("Gitea wiki update response must be an object")
        return updated

    def create_pull_request(self, *, title: str, body: str, head: str, base: str) -> dict[str, Any]:
        payload = {"title": title, "body": body, "head": head, "base": base}
        created = self._request_json("POST", f"/repos/{self._repo_path()}/pulls", payload=payload)
        if not isinstance(created, dict):
            raise GiteaError("Gitea create pull request response must be an object")
        return created

    def _request_json(
        self,
        method: str,
        api_path: str,
        *,
        query: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.config.base_url}/api/v1{api_path}"
        if query:
            url += "?" + parse.urlencode(query)
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = request.Request(url, data=body, method=method)
        req.add_header("Accept", "application/json")
        req.add_header("Authorization", f"token {self.config.token}")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with request.urlopen(req, timeout=30) as response:
                raw = response.read().decode("utf-8")
        except Exception as exc:  # pragma: no cover - network path is integration-tested by users
            raise GiteaError(str(exc)) from exc
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GiteaError("Gitea response was not JSON") from exc

    def _repo_path(self) -> str:
        return "/".join(parse.quote(part, safe="") for part in self.config.repo.split("/"))


def issue_number(issue: dict[str, Any]) -> int | None:
    raw = issue.get("number", issue.get("index", issue.get("id")))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _to_base64_text(value: str) -> str:
    import base64

    return base64.b64encode(value.encode("utf-8")).decode("ascii")
