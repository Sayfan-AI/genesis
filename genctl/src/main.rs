use clap::{Parser, Subcommand};

mod config;
mod issues;
mod log_cmd;

#[derive(Parser)]
#[command(name = "genctl", about = "CLI tool for genesis dev systems")]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Log agent activity to Grafana Loki
    Log(log_cmd::LogArgs),
    /// Communicate with humans via A2H protocol
    A2h {
        #[command(subcommand)]
        command: A2hCommands,
    },
    /// Manage issues (GitHub, Linear)
    Issues {
        #[command(subcommand)]
        command: IssueCommands,
    },
    /// Read configuration and secrets
    Config {
        #[command(subcommand)]
        command: ConfigCommands,
    },
}

#[derive(Subcommand)]
enum A2hCommands {
    /// Send a notification to the human
    Inform {
        #[arg(long)]
        message: String,
    },
    /// Request approval from the human
    Authorize {
        #[arg(long)]
        action: String,
        #[arg(long, default_value = "medium")]
        risk: String,
    },
    /// Collect input from the human
    Collect {
        #[arg(long)]
        question: String,
    },
    /// Escalate a blocker to the human
    Escalate {
        #[arg(long)]
        issue: Option<String>,
        #[arg(long)]
        reason: String,
    },
    /// Report task/milestone completion
    Result {
        #[arg(long)]
        milestone: Option<String>,
        #[arg(long)]
        status: String,
    },
}

#[derive(Subcommand)]
enum IssueCommands {
    /// Create a new issue
    Create {
        #[arg(long)]
        title: String,
        #[arg(long)]
        labels: Option<String>,
        #[arg(long)]
        body: Option<String>,
    },
    /// List issues
    List {
        #[arg(long)]
        status: Option<String>,
        #[arg(long)]
        milestone: Option<String>,
    },
    /// Close an issue
    Close {
        #[arg(long)]
        id: String,
        #[arg(long)]
        reason: Option<String>,
    },
    /// Assign an issue
    Assign {
        #[arg(long)]
        id: String,
        #[arg(long)]
        to: String,
    },
}

#[derive(Subcommand)]
enum ConfigCommands {
    /// Get a configuration value or secret
    Get {
        /// Key to look up
        key: String,
    },
}

fn main() {
    let cli = Cli::parse();

    match cli.command {
        Commands::Log(args) => log_cmd::run(args),
        Commands::A2h { command } => {
            // TODO: implement A2H client
            match command {
                A2hCommands::Inform { message } => {
                    eprintln!("[genctl] a2h inform: {message}");
                }
                A2hCommands::Authorize { action, risk } => {
                    eprintln!("[genctl] a2h authorize: {action} (risk: {risk})");
                }
                A2hCommands::Collect { question } => {
                    eprintln!("[genctl] a2h collect: {question}");
                }
                A2hCommands::Escalate { issue, reason } => {
                    eprintln!(
                        "[genctl] a2h escalate: {reason}{}",
                        issue.map(|i| format!(" (issue: {i})")).unwrap_or_default()
                    );
                }
                A2hCommands::Result { milestone, status } => {
                    eprintln!(
                        "[genctl] a2h result: {status}{}",
                        milestone.map(|m| format!(" (milestone: {m})")).unwrap_or_default()
                    );
                }
            }
        }
        Commands::Issues { command } => match command {
            IssueCommands::Create {
                title,
                labels,
                body,
            } => {
                issues::create(&title, labels.as_deref(), body.as_deref());
            }
            IssueCommands::List { status, milestone } => {
                issues::list(status.as_deref(), milestone.as_deref());
            }
            IssueCommands::Close { id, reason } => {
                issues::close(&id, reason.as_deref());
            }
            IssueCommands::Assign { id, to } => {
                issues::assign(&id, &to);
            }
        },
        Commands::Config { command } => {
            match command {
                ConfigCommands::Get { key } => {
                    // Try environment variable first, then config file
                    if let Ok(val) = std::env::var(&key) {
                        println!("{val}");
                    } else if let Ok(val) = std::env::var(format!("GENCTL_{key}")) {
                        println!("{val}");
                    } else {
                        match config::get_value(&key) {
                            Some(val) => println!("{val}"),
                            None => {
                                eprintln!("[genctl] config key not found: {key}");
                                std::process::exit(1);
                            }
                        }
                    }
                }
            }
        }
    }
}
