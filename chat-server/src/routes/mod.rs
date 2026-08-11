pub mod users;
pub mod groups;
pub mod messages;
pub mod internal;

use std::sync::Arc;
use crate::db::Database;

/// 共享的 AppState，用于所有需要数据库的处理器
#[derive(Clone)]
pub struct AppState {
    pub db: Arc<Database>,
}
