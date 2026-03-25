use clap::Args;
use serde::{Deserialize, Serialize};
use std::io::Read;

use crate::config;

#[derive(Args)]
pub struct LogArgs {
    /// Hook event name (session-start, post-tool-use, etc.)
    #[arg(long)]
    pub hook: String,

    /// Override agent name
    #[arg(long)]
    pub agent: Option<String>,

    /// Action description
    #[arg(long)]
    pub action: Option<String>,

    /// Related issue
    #[arg(long)]
    pub issue: Option<String>,

    /// Outcome (success, failure, blocked, escalated)
    #[arg(long)]
    pub outcome: Option<String>,
}

#[derive(Serialize)]
struct LokiPush {
    streams: Vec<LokiStream>,
}

#[derive(Serialize)]
struct LokiStream {
    stream: LokiLabels,
    values: Vec<[String; 2]>,
}

#[derive(Serialize)]
struct LokiLabels {
    project: String,
    hook_event: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    agent_type: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    tool_name: Option<String>,
}

/// Fields from CC hook stdin that we care about.
#[derive(Deserialize, Default)]
#[allow(dead_code)]
struct HookInput {
    session_id: Option<String>,
    hook_event_name: Option<String>,
    agent_type: Option<String>,
    tool_name: Option<String>,
    tool_input: Option<serde_json::Value>,
    tool_response: Option<serde_json::Value>,
    error: Option<String>,
    #[serde(default)]
    cwd: Option<String>,
}

pub fn run(args: LogArgs) {
    // Read hook context from stdin (CC pipes JSON to hooks)
    let hook_input = read_stdin_hook_input();

    let project = config::get_value("project.name").unwrap_or_else(|| "unknown".to_string());

    let agent_type = args
        .agent
        .clone()
        .or(hook_input.agent_type.clone());
    let tool_name = hook_input.tool_name.clone();

    // Build the log entry
    let entry = build_log_entry(&args, &hook_input, &project);

    let labels = LokiLabels {
        project,
        hook_event: args.hook.clone(),
        agent_type,
        tool_name,
    };

    let timestamp_ns = chrono::Utc::now()
        .timestamp_nanos_opt()
        .unwrap_or(0)
        .to_string();

    let push = LokiPush {
        streams: vec![LokiStream {
            stream: labels,
            values: vec![[timestamp_ns, entry]],
        }],
    };

    // Try to push to Loki
    if let Err(e) = push_to_loki(&push) {
        // Log failures go to stderr — don't break the agent
        eprintln!("[genctl] log push failed: {e}");
    }
}

fn read_stdin_hook_input() -> HookInput {
    // Check if stdin has data (non-blocking check via is_terminal)
    if atty_is_terminal() {
        return HookInput::default();
    }

    let mut buf = String::new();
    if std::io::stdin().read_to_string(&mut buf).is_ok() && !buf.trim().is_empty() {
        serde_json::from_str(&buf).unwrap_or_default()
    } else {
        HookInput::default()
    }
}

fn atty_is_terminal() -> bool {
    std::io::IsTerminal::is_terminal(&std::io::stdin())
}

fn build_log_entry(args: &LogArgs, input: &HookInput, project: &str) -> String {
    let mut parts: Vec<String> = vec![];

    parts.push(format!("hook={}", args.hook));
    parts.push(format!("project={project}"));

    if let Some(ref sid) = input.session_id {
        parts.push(format!("session={sid}"));
    }
    if let Some(ref agent) = input.agent_type {
        parts.push(format!("agent={agent}"));
    }
    if let Some(ref agent) = args.agent {
        parts.push(format!("agent={agent}"));
    }
    if let Some(ref tool) = input.tool_name {
        parts.push(format!("tool={tool}"));
    }
    if let Some(ref action) = args.action {
        parts.push(format!("action={action}"));
    }
    if let Some(ref issue) = args.issue {
        parts.push(format!("issue={issue}"));
    }
    if let Some(ref outcome) = args.outcome {
        parts.push(format!("outcome={outcome}"));
    }
    if let Some(ref err) = input.error {
        parts.push(format!("error={err}"));
    }
    if let Some(ref tool_input) = input.tool_input {
        // Compact JSON for the tool input
        if let Ok(s) = serde_json::to_string(tool_input) {
            if s.len() <= 500 {
                parts.push(format!("input={s}"));
            } else {
                parts.push(format!("input={}...", &s[..500]));
            }
        }
    }

    parts.join(" ")
}

fn push_to_loki(push: &LokiPush) -> Result<(), String> {
    let endpoint = std::env::var("GENCTL_LOKI_URL")
        .or_else(|_| config::get_value("loki.endpoint").ok_or("missing".to_string()))
        .map_err(|_| "no Loki endpoint configured")?;

    if endpoint.is_empty() {
        return Err("Loki endpoint is empty".to_string());
    }

    let url = format!("{}/loki/api/v1/push", endpoint.trim_end_matches('/'));

    let mut request = reqwest::blocking::Client::new()
        .post(&url)
        .header("Content-Type", "application/json")
        .json(push);

    // Add basic auth if configured
    let user = std::env::var("GENCTL_LOKI_USER")
        .ok()
        .or_else(|| config::get_value("loki.user"));
    let token = std::env::var("GENCTL_LOKI_TOKEN")
        .ok()
        .or_else(|| config::get_value("loki.token"));

    if let (Some(u), Some(t)) = (user, token) {
        if !u.is_empty() && !t.is_empty() {
            request = request.basic_auth(u, Some(t));
        }
    }

    let response = request.send().map_err(|e| e.to_string())?;

    if response.status().is_success() {
        Ok(())
    } else {
        Err(format!(
            "Loki returned {}",
            response.status()
        ))
    }
}
