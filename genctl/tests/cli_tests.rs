use std::process::Command;

fn genctl() -> Command {
    Command::new(env!("CARGO_BIN_EXE_genctl"))
}

#[test]
fn test_help_works() {
    let output = genctl().arg("--help").output().unwrap();
    assert!(output.status.success());
    let stdout = String::from_utf8(output.stdout).unwrap();
    assert!(stdout.contains("genesis dev systems"));
    assert!(stdout.contains("log"));
    assert!(stdout.contains("a2h"));
    assert!(stdout.contains("issues"));
    assert!(stdout.contains("config"));
}

#[test]
fn test_log_requires_hook() {
    let output = genctl().args(["log"]).output().unwrap();
    assert!(!output.status.success());
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(stderr.contains("--hook"));
}

#[test]
fn test_log_with_hook_runs() {
    // Without Loki configured, it should still run (log push fails gracefully)
    let output = genctl()
        .args(["log", "--hook", "session-start"])
        .output()
        .unwrap();
    // Exit 0 — log push failure is non-fatal
    assert!(output.status.success());
}

#[test]
fn test_log_with_piped_stdin() {
    use std::io::Write;
    let mut child = genctl()
        .args(["log", "--hook", "post-tool-use", "--agent", "test-agent"])
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .unwrap();

    let stdin = child.stdin.as_mut().unwrap();
    stdin
        .write_all(
            br#"{"session_id":"abc123","tool_name":"Bash","tool_input":{"command":"echo hi"}}"#,
        )
        .unwrap();
    drop(child.stdin.take());

    let output = child.wait_with_output().unwrap();
    assert!(output.status.success());
}

#[test]
fn test_a2h_inform() {
    let output = genctl()
        .args(["a2h", "inform", "--message", "test message"])
        .output()
        .unwrap();
    assert!(output.status.success());
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(stderr.contains("a2h inform: test message"));
}

#[test]
fn test_a2h_authorize() {
    let output = genctl()
        .args(["a2h", "authorize", "--action", "merge PR", "--risk", "low"])
        .output()
        .unwrap();
    assert!(output.status.success());
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(stderr.contains("merge PR"));
    assert!(stderr.contains("low"));
}

#[test]
fn test_a2h_escalate() {
    let output = genctl()
        .args([
            "a2h",
            "escalate",
            "--reason",
            "need credentials",
            "--issue",
            "42",
        ])
        .output()
        .unwrap();
    assert!(output.status.success());
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(stderr.contains("need credentials"));
    assert!(stderr.contains("42"));
}

// Issues commands now call real `gh` CLI, so we test argument parsing only.
// Full integration tests require a GitHub repo.

#[test]
fn test_issues_create_requires_title() {
    let output = genctl().args(["issues", "create"]).output().unwrap();
    assert!(!output.status.success());
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(stderr.contains("--title"));
}

#[test]
fn test_issues_close_requires_id() {
    let output = genctl().args(["issues", "close"]).output().unwrap();
    assert!(!output.status.success());
    let stderr = String::from_utf8(output.stderr).unwrap();
    assert!(stderr.contains("--id"));
}

#[test]
fn test_issues_assign_requires_id_and_to() {
    let output = genctl().args(["issues", "assign"]).output().unwrap();
    assert!(!output.status.success());
}

#[test]
fn test_issues_help() {
    let output = genctl().args(["issues", "--help"]).output().unwrap();
    assert!(output.status.success());
    let stdout = String::from_utf8(output.stdout).unwrap();
    assert!(stdout.contains("create"));
    assert!(stdout.contains("list"));
    assert!(stdout.contains("close"));
    assert!(stdout.contains("assign"));
}

#[test]
fn test_config_get_from_env() {
    let output = genctl()
        .args(["config", "get", "TEST_KEY"])
        .env("TEST_KEY", "test_value")
        .output()
        .unwrap();
    assert!(output.status.success());
    let stdout = String::from_utf8(output.stdout).unwrap();
    assert_eq!(stdout.trim(), "test_value");
}

#[test]
fn test_config_get_from_genctl_prefix_env() {
    let output = genctl()
        .args(["config", "get", "MY_SETTING"])
        .env("GENCTL_MY_SETTING", "prefixed_value")
        .output()
        .unwrap();
    assert!(output.status.success());
    let stdout = String::from_utf8(output.stdout).unwrap();
    assert_eq!(stdout.trim(), "prefixed_value");
}

#[test]
fn test_config_get_missing_key_fails() {
    let output = genctl()
        .args(["config", "get", "NONEXISTENT_KEY_12345"])
        .env_remove("NONEXISTENT_KEY_12345")
        .env_remove("GENCTL_NONEXISTENT_KEY_12345")
        .output()
        .unwrap();
    assert!(!output.status.success());
}
