Feature: Comprehensive pull-request review
  As a QA owner reviewing a branch and pull request
  I want deterministic code review plus generated case execution
  So that a passing regression suite cannot hide black-box, boundary, stress, or documentation gaps

  Background:
    Given a pinned repository and pull-request head
    And the review workflow has a local-only write gate

  Rule: Configuration discovery before comprehensive execution

    @planned @current @supported @code-review @review @ux
    Scenario: Review offers one confirmation when the effective product contract is missing
      Given repository and existing test analysis found a product runner or Browser test surface
      And no normalized product execution contract is available
      When comprehensive review is requested
      Then review shows the discovered runner, local/remote execution target, URL discovery policy, and candidate semantic steps
      And review asks for one explicit confirmation
      And review does not require hand-authored Quality Pilot YAML
      And declining confirmation returns CONFIGURATION_REQUIRED without executing product commands

    @planned @current @supported @code-review @review @remote @provenance
    Scenario: Review reports local and remote evidence origins separately
      Given white-box tests run in a local disposable pinned worktree
      And product Browser UI runs on a remote target through a local Playwright SSH tunnel
      When the review report is rendered
      Then each case identifies execution_target and evidence_origin
      And local white-box evidence is not described as remote product evidence
      And remote product evidence is not described as local worktree evidence

  Rule: PR identity and diff authority

    @current @supported @code-review @review @diff
    Scenario: An empty MCP diff is reconstructed from the pinned base and head
      Given the PR snapshot has changed files but an empty diff field
      When the review reconstructs the diff from the pinned commits
      Then the diff source is git_reconstructed
      And diff findings use the reconstructed content

  Rule: 共用測試執行與證據邊界

    @current @supported @code-review @review @case-run @product-test
    Scenario: Product testing reuses the same confirmed environment and evidence boundary as case tests
      Given a confirmed local product environment
      And a user-owned product testing contract with a build recipe and semantic operation
      When comprehensive review runs the product test adapter
      Then the build and product operation use the same pinned worktree boundary as case execution
      And command safety validation uses the same allowlisted argv policy
      And build, product operation, and case results use redacted evidence with traceable statuses
      And product test PASS is not inferred from case test PASS

    @current @supported @code-review @review @case-run @browser
    Scenario: Browser UI product testing shares case-test evidence rules but keeps a browser-specific oracle
      Given a confirmed local product environment with Playwright and Chromium available
      And a browser UI contract with at least one interaction and one semantic assertion
      When comprehensive review runs the browser product test
      Then the real browser flow uses the same environment confirmation, timeout, redaction, and evidence rules as case execution
      And screenshot, trace, console, and server logs remain linked to the browser result
      And a browser result is not replaced by a curl, API probe, mock DOM, or generic case probe

    @current @supported @code-review @review @case-run @dependency
    Scenario: Missing browser dependency does not hide independent case tests
      Given the confirmed local environment is missing the Playwright Python package
      When comprehensive review prepares declared dependencies and runs tests
      Then the dependency installation result is recorded separately from test results
      And browser tests are BLOCK or PARTIAL with the original collection error
      And independent non-browser cases are still attempted when safe
      And a partial fallback result is not reported as a full regression PASS

  Rule: Comprehensive QA matrix

    @current @supported @code-review @review @case-generation @case-run
    Scenario: Comprehensive review invokes case generation and case execution
      Given a ready PR review worktree with mocked generated contracts
      When the comprehensive review workflow runs
      Then the case generator is called with the pinned PR context
      And the case runner is called for each generated contract

    @current @supported @code-review @review @case-generation @case-run
    Scenario: Comprehensive review records every required QA dimension
      Given generated case results for functional, boundary, and stress dimensions
      When the comprehensive review QA matrix is built
      Then it contains black_box, white_box, functional, boundary, stress, and documentation dimensions
      And each dimension has an explicit status
      And generated case evidence remains linked to its case result

    @current @supported @code-review @review @human-gate
    Scenario: QA coverage gaps produce an advisory comment instead of approval
      Given the comprehensive QA outcome is HOLD
      When a review reply is prepared with explicit confirmation
      Then the remote reply is prepared as an advisory COMMENT
      And the approval decision remains user-owned
      And no remote apply is allowed

    @current @supported @code-review @review @attachment
    Scenario: Browser evidence attachment requires a separate gated upload capability
      Given a browser test produced a redacted screenshot or trace
      When the review prepares a Gitea advisory comment
      Then local browser evidence remains linked to the browser result
      And a remote image is included only when an explicit Gitea attachment upload gate succeeds
      And a local filesystem path is never emitted as a remote image URL
      And the remote review state remains COMMENT and user-owned

    @current @supported @code-review @review
    Scenario: Diff-only review is explicit and does not pretend to be comprehensive
      Given the reviewer selects diff-only mode
      When the review QA plan is created
      Then its mode is diff_only
      And its outcome is NOT_RUN
      And no generated case is reported as executed
