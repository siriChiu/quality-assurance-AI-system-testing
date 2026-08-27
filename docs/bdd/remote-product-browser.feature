Feature: Remote product and browser execution boundary
  As a QA owner reviewing a pinned pull request
  I want local review, remote product execution, and Playwright evidence to have explicit boundaries
  So that a remote observation cannot be confused with local white-box evidence or a different source revision

  Background:
    Given a pinned repository, pull-request head SHA, and review run ID
    And the review has a normalized effective execution contract
    And raw secrets are rejected before command, URL, trace, or evidence persistence

  Rule: Configuration discovery and confirmation UX

    @planned @setup @doctor @contract @ux
    Scenario: Setup discovers a product Browser runner without requiring YAML authoring
      Given the repository contains README or --help evidence for main.py --browser
      And the repository contains existing Playwright or browser UI tests
      When the user runs setup
      Then Quality Pilot displays the discovered runner, URL discovery mode, Browser test count, and evidence boundary
      And setup does not require the user to hand-write runtime.product_testing.web_ui
      And setup asks for one explicit confirmation before writing a normalized contract

    @planned @setup @doctor @environment @remote @ux
    Scenario: Remote configuration discovery prefers known facts and asks only for missing facts
      Given an existing config contains a remote host, repository, or Python path
      And an SSH config alias or git remote provides additional non-secret hints
      When Quality Pilot builds the environment candidate
      Then existing confirmed settings take precedence
      And SSH aliases and repository metadata are used only as discovery hints
      And the user is asked only for facts that remain unverified
      And raw passwords, tokens, and private keys are never requested or persisted

    @planned @setup @review @browser @ux
    Scenario: Existing Browser tests produce candidate semantic steps
      Given tests/browser_ui contains Playwright interactions and assertions
      When Quality Pilot analyzes the Browser test surface
      Then it extracts candidate navigation, locator, interaction, and semantic assertion steps
      And the candidates retain source test paths and line references
      And candidate steps are not official QA evidence until the user confirms their scope

    @planned @review @contract @ux
    Scenario: Review starts a one-time contract wizard when product settings are missing
      Given review discovers that no normalized product Browser contract exists
      When the user runs comprehensive review
      Then review presents the discovered product runner, remote/local target, URL discovery, and candidate Browser steps
      And the user can confirm once to create the normalized contract
      And review continues without requiring a separate manual YAML edit
      And declining confirmation returns CONFIGURATION_REQUIRED without executing product or Browser commands

  Rule: One effective product contract

    @planned @code-review @review @contract @remote
    Scenario: Legacy and nested Browser UI settings are normalized into one contract
      Given runtime.web_ui contains a Browser UI candidate
      And runtime.product_testing.web_ui is absent or null
      When the product execution contract is normalized
      Then the effective contract contains exactly one web_ui definition
      And the contract records the source of the selected settings
      And the contract hash covers the effective web_ui, execution target, and evidence policy
      And execution does not independently re-read a different web_ui section

    @planned @code-review @review @contract @remote
    Scenario: A README or help command remains a candidate until explicitly promoted
      Given README or --help analysis finds main.py --browser
      When the runtime profile is generated
      Then the command is recorded as an executable candidate
      And candidate analysis does not silently rewrite the official product contract
      And a runnable Browser case requires an explicit user-owned contract or confirmation

  Rule: Remote preflight and source authority

    @planned @code-review @review @environment @remote @evidence-completeness
    Scenario: Remote fixture paths are never checked with local Path.exists
      Given execution target is remote_ssh
      And the fixture path is absolute on the remote host
      When environment readiness is evaluated locally
      Then the local checker does not emit fixture_missing for that remote path
      And readiness is REMOTE_PREFLIGHT_REQUIRED until remote preflight succeeds

    @planned @code-review @review @environment @remote @evidence-completeness
    Scenario: A failed or stale remote preflight cannot make the environment ready
      Given a stored remote preflight has status TOOLING_FAIL, INFRASTRUCTURE_BLOCK, or an expired timestamp
      When environment readiness is evaluated
      Then ready is false
      And the status is REMOTE_PREFLIGHT_REQUIRED or INFRASTRUCTURE_BLOCK
      And the failed or stale result is not accepted as remote readiness

    @planned @code-review @review @environment @remote @evidence-completeness
    Scenario: Remote preflight reports transport, dependency, browser, and source checks independently
      Given a configured SSH alias, remote repository, remote Python, fixture, and expected PR head SHA
      When remote preflight executes
      Then it records independent checks for ssh, remote_repo, remote_python, remote_fixture, remote_requirements, remote_playwright, remote_chromium, remote_source_commit, and remote_source_dirty
      And it writes a redacted remote-preflight evidence artifact
      And no password, private key, token, or raw credential is persisted

    @planned @code-review @review @environment @remote @freshness
    Scenario: Remote source must match the pinned PR before official product evidence
      Given the review expects head SHA H
      When remote preflight reports a remote HEAD different from H
      Then the remote product result is REMOTE_SOURCE_MISMATCH
      And remote Browser observations are not official PR evidence
      And the review does not claim PASS, APPROVED, or MERGE_ALLOWED from that observation

    @planned @code-review @review @environment @remote @freshness
    Scenario: A dirty remote source cannot become official evidence
      Given the review expects head SHA H
      And remote preflight reports remote HEAD H
      And remote source dirty state is true
      When remote product/browser readiness is evaluated
      Then the status is REMOTE_SOURCE_DIRTY or INFRASTRUCTURE_BLOCK
      And product evaluation is NOT_EVALUATED
      And the dirty remote observation is not official PR evidence

    @planned @code-review @review @environment @remote @freshness
    Scenario: A clean remote source at the pinned PR head may become official evidence
      Given the review expects head SHA H
      And remote preflight reports remote HEAD H
      And remote source dirty state is false
      And all required remote preflight checks pass
      When the remote product/browser flow executes
      Then evidence_origin is remote
      And source_identity.status is VERIFIED
      And the result may participate in the official PR QA matrix

  Rule: Independent local and remote execution stages

    @planned @code-review @review @remote @case-run
    Scenario: Local white-box tests and remote product Browser tests use separate execution targets
      Given local_review_worktree and local_pytest are enabled
      And product_target is remote_ssh
      And playwright_target is local_via_ssh_tunnel
      When comprehensive review is planned
      Then white-box test commands use the disposable pinned local worktree
      And the product Browser server uses the configured remote repository and remote Python
      And the local Playwright client uses only the SSH tunnel endpoint
      And every case records its execution_target and evidence_origin

    @planned @code-review @review @remote @case-run
    Scenario: Product build BLOCK does not prevent an independent remote Browser case
      Given the remote product target is ready
      And the product build contract is missing or product build returns BLOCK
      And a valid remote Browser contract exists
      When comprehensive review executes the cases
      Then the product build case is BLOCK
      And the remote Browser case is still attempted
      And the composite product outcome remains BLOCK
      And the Browser matrix records its own PASS, FAIL, or BLOCK result

  Rule: Remote Browser lifecycle

    @planned @code-review @review @remote @browser @case-run
    Scenario: Remote Browser starts the real product and discovers a dynamic URL
      Given the Browser start argv is main.py --browser
      And url_discovery source is stdout
      And the remote product prints a dynamic host, port, and tokenized URL
      When the remote Browser adapter starts the product
      Then it starts the configured remote product rather than a generic server or dummy command
      And it parses the URL in memory
      And it creates an SSH tunnel to the discovered remote host and port
      And Playwright opens the localhost tunnel URL
      And the raw token is never persisted

    @planned @code-review @review @remote @browser @case-run
    Scenario: Playwright runs through a prestarted server session
      Given the remote product process and SSH tunnel are ready
      When the Playwright session starts
      Then the Browser adapter does not start a second local product subprocess
      And it executes real click, fill, navigation, and semantic state assertions
      And the result links screenshot, sanitized trace, interaction, console, network, and DOM evidence

    @planned @code-review @review @remote @browser @evidence-completeness
    Scenario: Tokenized Browser trace is sanitized before persistence
      Given the discovered URL contains a per-run token
      When Playwright tracing and Browser diagnostics finish
      Then temporary raw trace data is not exposed as canonical evidence
      And URL query values, authorization headers, console output, network URLs, and DOM snapshots are redacted
      And only the sanitized trace receives an evidence hash

    @planned @code-review @review @remote @browser @human-gate
    Scenario: Remote Browser process and tunnel are cleaned up after every outcome
      Given the remote Browser flow is running
      When the flow passes, fails, times out, or is interrupted
      Then the SSH tunnel is terminated
      And the remote product process group is terminated and verified
      And cleanup failure is recorded as REMOTE_PROCESS_CLEANUP_FAILED
      And cleanup failure is not hidden by a Browser PASS

  Rule: Browser failure classification

    @planned @code-review @review @remote @browser @evidence-completeness
    Scenario: A Playwright timeout is diagnostically classified before product FAIL
      Given a Browser interaction times out
      When the adapter collects the failure snapshot
      Then it records screenshot, DOM, disabled, aria-disabled, computed visibility, overlay, pending network, console, URL, and viewport facts when available
      And it classifies the timeout as PRODUCT_UI_FAILURE, HARNESS_INTERACTION_FAILURE, BROWSER_STARTUP_BLOCK, ORACLE_MISSING, or TIMEOUT_UNCLASSIFIED
      And an unresolved timeout is not promoted to a product defect

    @planned @code-review @review @remote @browser
    Scenario: A pinned-worktree path assertion is separated from a product UI assertion
      Given a local Browser test expects the source repository root name
      And the test runs in a disposable pinned worktree with a different directory name
      When the test fails on the root path assertion
      Then the failure is reported in harness_health or source_path_contract
      And it does not automatically become PRODUCT_UI_FAILURE
      And independent semantic Browser failures remain separately classified

  Rule: Canonical lineage and report origin

    @planned @code-review @review @remote @case-run @provenance
    Scenario: Product and Browser contracts remain available after execution
      Given a product build case and a child Browser case are executed
      When the review persists canonical artifacts
      Then both CaseContract files remain available
      And each canonical result.json contains case_id, case_type, contract_hash, run_id, oracle, status, truth_status, and evidence
      And the child Browser result links to its parent case
      And no later case-generation cleanup deletes an executed contract

    @planned @code-review @review @remote @case-run @provenance
    Scenario: Graph projection accepts only verified canonical case results
      Given a review report contains a case entry
      When Knowledge Graph projection validates the case
      Then result_path must exist and resolve to canonical result.json
      And case_id, run_id, and contract_hash must match the result
      And every evidence reference must be validated or explicitly marked unavailable
      And a matrix-only entry cannot create a TestRun or Evidence authority
