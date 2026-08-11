use crate::auth::AuthUser;
use crate::error::AppError;
use crate::models::*;
use axum::{
    extract::{Path, State},
    routing::{get, post},
    Json, Router,
};
use base64::Engine;

use super::AppState;

/// 需要认证的路由
pub fn authed_routes() -> Router<AppState> {
    Router::new()
        .route("/api/v1/groups", post(create_group))
        .route("/api/v1/groups/list", get(list_groups))
        .route("/api/v1/groups/:id/join", post(join_group))
        .route(
            "/api/v1/groups/:id/members",
            get(get_members).post(add_member),
        )
        // 兼容旧路径
        .route("/api/groups", post(create_group))
        .route("/api/groups/list", get(list_groups))
        .route("/api/groups/:id/join", post(join_group))
        .route("/api/groups/:id/members", get(get_members).post(add_member))
}

/// POST /api/v1/groups — 创建群组
async fn create_group(
    State(state): State<AppState>,
    AuthUser(user): AuthUser,
    Json(req): Json<CreateGroupRequest>,
) -> Result<Json<serde_json::Value>, AppError> {
    if req.name.trim().is_empty() || req.name.len() > 64 {
        return Err(AppError::BadRequest("群组名需为 1-64 字符".to_string()));
    }

    if req.member_ids.len() != req.encrypted_group_keys.len() {
        return Err(AppError::BadRequest("成员与密钥数量不匹配".to_string()));
    }
    if req.member_ids.len() < 2 {
        return Err(AppError::BadRequest(
            "群组至少需要创建者和一名好友".to_string(),
        ));
    }
    if !req.member_ids.contains(&user.id) {
        return Err(AppError::BadRequest("创建者必须包含在群成员中".to_string()));
    }
    let mut unique_members = std::collections::HashSet::new();
    for &member_id in &req.member_ids {
        if !unique_members.insert(member_id) {
            return Err(AppError::BadRequest("群成员不能重复".to_string()));
        }
        if member_id != user.id && !state.db.are_friends(user.id, member_id)? {
            return Err(AppError::BadRequest("只能邀请好友加入群组".to_string()));
        }
        state.db.get_user_by_id(member_id)?;
    }

    // 解码 base64 的群密钥
    let keys: Vec<Vec<u8>> = req
        .encrypted_group_keys
        .iter()
        .map(|k| {
            base64::engine::general_purpose::STANDARD
                .decode(k)
                .map_err(|_| AppError::BadRequest("群密钥 base64 解码失败".to_string()))
        })
        .collect::<Result<Vec<_>, _>>()?;

    let group_id = state
        .db
        .create_group(&req.name, user.id, &req.member_ids, &keys)?;

    Ok(Json(serde_json::json!({
        "group_id": group_id,
        "name": req.name
    })))
}

/// GET /api/v1/groups/list — 获取用户所在群列表
async fn list_groups(
    State(state): State<AppState>,
    AuthUser(user): AuthUser,
) -> Result<Json<Vec<Group>>, AppError> {
    let groups = state.db.get_user_groups(user.id)?;
    Ok(Json(groups))
}

/// POST /api/v1/groups/{id}/join — 加入群组（提供用自己公钥加密的群密钥）
async fn join_group(
    State(state): State<AppState>,
    AuthUser(user): AuthUser,
    Path(id): Path<i64>,
    Json(_req): Json<JoinGroupRequest>,
) -> Result<Json<serde_json::Value>, AppError> {
    let _ = (state, user, id);
    Err(AppError::BadRequest(
        "请由群创建者通过成员接口添加好友".to_string(),
    ))
}

async fn add_member(
    State(state): State<AppState>,
    AuthUser(user): AuthUser,
    Path(id): Path<i64>,
    Json(req): Json<AddGroupMemberRequest>,
) -> Result<Json<serde_json::Value>, AppError> {
    let group = state.db.get_group(id)?;
    if group.creator_id != user.id {
        return Err(AppError::Unauthorized(
            "只有群创建者可以添加成员".to_string(),
        ));
    }
    if !state.db.are_friends(user.id, req.user_id)? {
        return Err(AppError::BadRequest("只能添加好友".to_string()));
    }
    state
        .db
        .add_group_member(id, req.user_id, &req.encrypted_key)?;
    Ok(Json(serde_json::json!({"status": "ok", "group_id": id})))
}

/// GET /api/v1/groups/{id}/members — 获取群成员列表（含加密的群密钥）
async fn get_members(
    State(state): State<AppState>,
    AuthUser(user): AuthUser,
    Path(id): Path<i64>,
) -> Result<Json<Vec<GroupMember>>, AppError> {
    // 验证用户是群成员
    let members = state.db.get_group_members(id)?;
    if !members.iter().any(|m| m.user_id == user.id) {
        return Err(AppError::BadRequest("你不是该群成员".to_string()));
    }
    Ok(Json(members))
}
