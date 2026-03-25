use std::process::Command;

use serde::Deserialize;

#[derive(Deserialize)]
struct GhIssue {
    number: u64,
    title: String,
    state: String,
    url: String,
}

fn run_gh(args: &[&str], json: bool) -> Result<String, String> {
    let mut cmd = Command::new("gh");
    cmd.args(args);
    if json {
        cmd.args(["--json", "number,title,state,url"]);
    }

    let output = cmd.output().map_err(|e| {
        if e.kind() == std::io::ErrorKind::NotFound {
            "gh CLI not found. Install it: https://cli.github.com/".to_string()
        } else {
            format!("failed to run gh: {e}")
        }
    })?;

    if output.status.success() {
        Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
    } else {
        Err(String::from_utf8_lossy(&output.stderr).trim().to_string())
    }
}

pub fn create(title: &str, labels: Option<&str>, body: Option<&str>) {
    let mut args = vec!["issue", "create", "--title", title];

    if let Some(l) = labels {
        args.extend(["--label", l]);
    }
    if let Some(b) = body {
        args.extend(["--body", b]);
    }

    match run_gh(&args, false) {
        Ok(url) => println!("{url}"),
        Err(e) => {
            eprintln!("[genctl] issues create failed: {e}");
            std::process::exit(1);
        }
    }
}

pub fn list(status: Option<&str>, milestone: Option<&str>) {
    let state = status.unwrap_or("open");
    let mut args = vec!["issue", "list", "--state", state];

    // If milestone is specified, filter by label
    let milestone_label;
    if let Some(m) = milestone {
        milestone_label = format!("milestone:{m}");
        args.extend(["--label", &milestone_label]);
    }

    match run_gh(&args, true) {
        Ok(output) => {
            if output.is_empty() {
                println!("No issues found.");
                return;
            }
            match serde_json::from_str::<Vec<GhIssue>>(&output) {
                Ok(issues) => {
                    for issue in &issues {
                        println!(
                            "#{} [{}] {} ({})",
                            issue.number, issue.state, issue.title, issue.url
                        );
                    }
                }
                Err(_) => println!("{output}"),
            }
        }
        Err(e) => {
            eprintln!("[genctl] issues list failed: {e}");
            std::process::exit(1);
        }
    }
}

pub fn close(id: &str, reason: Option<&str>) {
    let mut args = vec!["issue", "close", id];

    if let Some(r) = reason {
        args.extend(["--reason", r]);
    }

    match run_gh(&args, false) {
        Ok(output) => {
            if !output.is_empty() {
                println!("{output}");
            }
            eprintln!("[genctl] issue {id} closed");
        }
        Err(e) => {
            eprintln!("[genctl] issues close failed: {e}");
            std::process::exit(1);
        }
    }
}

pub fn assign(id: &str, to: &str) {
    match run_gh(&["issue", "edit", id, "--add-assignee", to], false) {
        Ok(_) => eprintln!("[genctl] issue {id} assigned to {to}"),
        Err(e) => {
            eprintln!("[genctl] issues assign failed: {e}");
            std::process::exit(1);
        }
    }
}
