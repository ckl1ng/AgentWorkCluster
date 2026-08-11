use std::path::PathBuf;

pub struct Config {
    pub host: String,
    pub port: u16,
    pub db_path: PathBuf,
    pub agent_service_secret: Option<String>,
}

impl Config {
    pub fn from_env() -> Self {
        let host = std::env::var("HOST").unwrap_or_else(|_| "0.0.0.0".to_string());
        let port = std::env::var("PORT")
            .ok()
            .and_then(|p| p.parse().ok())
            .unwrap_or(9010);
        let db_path = std::env::var("DATA_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|_| PathBuf::from("./data"))
            .join("chat.db");
        let agent_service_secret = std::env::var("AGENT_SERVICE_SECRET")
            .ok()
            .filter(|value| !value.trim().is_empty());

        Self {
            host,
            port,
            db_path,
            agent_service_secret,
        }
    }
}
