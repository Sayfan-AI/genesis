use std::path::PathBuf;

/// Find the .genesis/config.toml by walking up from the current directory.
fn find_config_file() -> Option<PathBuf> {
    let mut dir = std::env::current_dir().ok()?;
    loop {
        let candidate = dir.join(".genesis").join("config.toml");
        if candidate.exists() {
            return Some(candidate);
        }
        if !dir.pop() {
            return None;
        }
    }
}

/// Get a value from .genesis/config.toml by dotted key path.
/// Supports keys like "project.name", "loki.endpoint", "issues.backend".
pub fn get_value(key: &str) -> Option<String> {
    let config_path = find_config_file()?;
    let content = std::fs::read_to_string(config_path).ok()?;
    let table: toml::Table = content.parse().ok()?;

    let parts: Vec<&str> = key.split('.').collect();
    let mut current: &toml::Value = &toml::Value::Table(table);

    for part in &parts {
        current = current.as_table()?.get(*part)?;
    }

    match current {
        toml::Value::String(s) => Some(s.clone()),
        toml::Value::Integer(i) => Some(i.to_string()),
        toml::Value::Float(f) => Some(f.to_string()),
        toml::Value::Boolean(b) => Some(b.to_string()),
        other => Some(other.to_string()),
    }
}
