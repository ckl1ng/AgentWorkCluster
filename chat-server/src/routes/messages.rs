use axum::{
    extract::{Path, State},
    routing::get,
    Json, Router,
};
use serde::Deserialize;

use crate::auth::AuthUser;
use crate::error::AppError;

use super::AppState;

#[derive(Deserialize)]
pub struct HistoryQuery {
    pub limit: Option<i64>,
    /// 游标分页：返回此 ID 之前的消息
    pub before_id: Option<i64>,
    /// 游标分页：返回此 ID 之后的消息
    pub after_id: Option<i64>,
}

/// 需要认证的路由
pub fn authed_routes() -> Router<AppState> {
    Router::new()
        .route("/api/v1/messages/:user_id", get(get_private_history))
        .route("/api/v1/groups/:id/messages", get(get_group_history))
        // 兼容旧路径
        .route("/api/messages/:user_id", get(get_private_history))
        .route("/api/groups/:id/messages", get(get_group_history))
}

/// GET /api/v1/messages/{user_id}?limit=50&before_id=100&after_id=90
/// 获取与某个用户的私聊历史（密文），支持游标分页
async fn get_private_history(
    State(state): State<AppState>,
    AuthUser(user): AuthUser,
    Path(user_id): Path<i64>,
    axum::extract::Query(q): axum::extract::Query<HistoryQuery>,
) -> Result<Json<serde_json::Value>, AppError> {
    if !state.db.are_friends(user.id, user_id)? {
        return Err(AppError::BadRequest("只能查看好友之间的消息".to_string()));
    }
    let limit = q.limit.unwrap_or(50).clamp(1, 200);
    let msgs = state.db.get_private_messages_paginated(
        user.id,
        user_id,
        limit,
        q.before_id,
        q.after_id,
    )?;
    Ok(Json(serde_json::json!({
        "messages": msgs,
        "limit": limit,
    })))
}

/// GET /api/v1/groups/{id}/messages?limit=50&before_id=100
/// 获取群聊历史（密文），支持游标分页
async fn get_group_history(
    State(state): State<AppState>,
    AuthUser(user): AuthUser,
    Path(id): Path<i64>,
    axum::extract::Query(q): axum::extract::Query<HistoryQuery>,
) -> Result<Json<serde_json::Value>, AppError> {
    state.db.get_group(id)?;
    // 验证用户是群成员
    let members = state.db.get_group_members(id)?;
    if !members.iter().any(|m| m.user_id == user.id) {
        return Err(AppError::BadRequest("你不是该群成员".to_string()));
    }
    let limit = q.limit.unwrap_or(50).clamp(1, 200);
    let msgs = state
        .db
        .get_group_messages_paginated(id, limit, q.before_id, q.after_id)?;
    Ok(Json(serde_json::json!({
        "messages": msgs,
        "limit": limit,
    })))
}
