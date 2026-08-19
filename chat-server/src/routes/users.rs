use axum::{
    extract::{Path, Query, State},
    routing::{get, post, put},
    Json, Router,
};

use crate::auth::AuthUser;
use crate::crypto;
use crate::error::AppError;
use crate::models::*;
use base64::Engine;

use super::AppState;

/// 公开路由（无需认证）
pub fn public_routes(state: AppState) -> Router {
    Router::new()
        .route("/api/v1/register", post(register))
        .route("/api/v1/login", post(login))
        // 兼容旧路径
        .route("/api/register", post(register))
        .route("/api/login", post(login))
        .with_state(state)
}

/// 需要认证的路由
pub fn authed_routes() -> Router<AppState> {
    Router::new()
        .route("/api/v1/users", get(list_users))
        .route("/api/v1/users/search", get(search_users))
        .route("/api/v1/friends", get(list_friends))
        .route(
            "/api/v1/friends/requests",
            get(list_friend_requests).post(create_friend_request),
        )
        .route(
            "/api/v1/friends/requests/:user_id/accept",
            post(accept_friend_request),
        )
        .route("/api/v1/users/me", get(me))
        .route("/api/v1/users/me/avatar", put(update_avatar))
        .route("/api/v1/users/:id", get(get_user))
        .route("/api/v1/users/:id/public_key", get(get_public_key))
        // 兼容旧路径
        .route("/api/users", get(list_users))
        .route("/api/users/search", get(search_users))
        .route("/api/friends", get(list_friends))
        .route(
            "/api/friends/requests",
            get(list_friend_requests).post(create_friend_request),
        )
        .route(
            "/api/friends/requests/:user_id/accept",
            post(accept_friend_request),
        )
        .route("/api/users/me", get(me))
        .route("/api/users/me/avatar", put(update_avatar))
        .route("/api/users/:id", get(get_user))
        .route("/api/users/:id/public_key", get(get_public_key))
}

/// POST /api/v1/register — 注册新用户
async fn register(
    State(state): State<AppState>,
    Json(req): Json<RegisterRequest>,
) -> Result<Json<RegisterResponse>, AppError> {
    // 验证公钥长度（32 字节 = Curve25519）
    if !crypto::validate_public_key(&req.public_key) {
        return Err(AppError::BadRequest(
            "公钥格式无效，需要 32 字节".to_string(),
        ));
    }

    if req.username.trim().is_empty() || req.username.len() > 32 {
        return Err(AppError::BadRequest("用户名需为 1-32 字符".to_string()));
    }
    if req.password.len() < 8 || req.password.len() > 128 {
        return Err(AppError::BadRequest("密码需为 8-128 字符".to_string()));
    }
    if req.encrypted_secret_key.is_empty() || req.encrypted_secret_key.len() > 512 {
        return Err(AppError::BadRequest("加密密钥备份格式无效".to_string()));
    }

    let token = crypto::generate_token();
    let password_hash = bcrypt::hash(&req.password, bcrypt::DEFAULT_COST)
        .map_err(|_| AppError::Internal("密码处理失败".to_string()))?;
    let user_id = state.db.register_user(
        req.username.trim(),
        &req.public_key,
        &token,
        &password_hash,
        &req.encrypted_secret_key,
    )?;

    Ok(Json(RegisterResponse {
        id: user_id,
        username: req.username.trim().to_string(),
        token,
    }))
}

/// POST /api/v1/login — 使用用户名和密码换取现有认证 token
async fn login(
    State(state): State<AppState>,
    Json(req): Json<LoginRequest>,
) -> Result<Json<LoginResponse>, AppError> {
    let username = req.username.trim();
    if username.is_empty() || req.password.is_empty() {
        return Err(AppError::BadRequest("请输入用户名和密码".to_string()));
    }
    let user = state.db.authenticate_password(username, &req.password)?;
    let token = crypto::generate_token();
    state.db.replace_user_token(user.id, &token)?;
    Ok(Json(LoginResponse {
        id: user.id,
        username: user.username,
        token,
    }))
}

/// GET /api/v1/users — 获取所有用户列表
async fn list_users(
    State(state): State<AppState>,
    AuthUser(user): AuthUser,
) -> Result<Json<Vec<User>>, AppError> {
    let users = state.db.list_friends(user.id)?;
    Ok(Json(users))
}

#[derive(serde::Deserialize)]
struct SearchQuery {
    q: String,
}

async fn search_users(
    State(state): State<AppState>,
    AuthUser(user): AuthUser,
    Query(query): Query<SearchQuery>,
) -> Result<Json<Vec<UserSearchResult>>, AppError> {
    let term = query.q.trim();
    if term.len() < 2 || term.len() > 32 {
        return Err(AppError::BadRequest("搜索关键词需为 2-32 字符".to_string()));
    }
    let results = state.db.search_users(term, user.id)?;
    Ok(Json(
        results
            .into_iter()
            .map(|u| UserSearchResult {
                id: u.id,
                username: u.username,
            })
            .collect(),
    ))
}

async fn list_friends(
    State(state): State<AppState>,
    AuthUser(user): AuthUser,
) -> Result<Json<Vec<User>>, AppError> {
    Ok(Json(state.db.list_friends(user.id)?))
}

async fn create_friend_request(
    State(state): State<AppState>,
    AuthUser(user): AuthUser,
    Json(req): Json<FriendRequestBody>,
) -> Result<Json<serde_json::Value>, AppError> {
    state.db.create_friend_request(user.id, req.user_id)?;
    Ok(Json(serde_json::json!({"status": "pending"})))
}

async fn list_friend_requests(
    State(state): State<AppState>,
    AuthUser(user): AuthUser,
) -> Result<Json<Vec<User>>, AppError> {
    Ok(Json(state.db.list_friend_requests(user.id)?))
}

#[derive(serde::Deserialize)]
struct AcceptPath {
    user_id: i64,
}

async fn accept_friend_request(
    State(state): State<AppState>,
    AuthUser(user): AuthUser,
    Path(path): Path<AcceptPath>,
) -> Result<Json<serde_json::Value>, AppError> {
    state.db.accept_friend_request(user.id, path.user_id)?;
    Ok(Json(serde_json::json!({"status": "accepted"})))
}

/// GET /api/v1/users/me — 获取当前用户信息
async fn me(
    State(state): State<AppState>,
    AuthUser(user): AuthUser,
) -> Result<Json<User>, AppError> {
    // 直接从数据库获取最新信息
    let user = state.db.get_user_by_id(user.id)?;
    Ok(Json(user))
}

async fn update_avatar(
    State(state): State<AppState>,
    AuthUser(user): AuthUser,
    Json(req): Json<UpdateAvatarRequest>,
) -> Result<Json<User>, AppError> {
    const MAX_AVATAR_DATA_URL_BYTES: usize = 512 * 1024;
    let avatar = req.avatar.trim();
    if avatar.len() > MAX_AVATAR_DATA_URL_BYTES {
        return Err(AppError::BadRequest("头像图片不能超过 512 KiB".to_string()));
    }
    if !avatar.is_empty()
        && !["data:image/jpeg;base64,", "data:image/png;base64,", "data:image/webp;base64,", "data:image/gif;base64,"]
            .iter()
            .any(|prefix| avatar.starts_with(prefix))
    {
        return Err(AppError::BadRequest("头像必须是图片数据".to_string()));
    }
    Ok(Json(state.db.update_user_avatar(user.id, avatar)?))
}

/// GET /api/v1/users/{id} — 获取用户信息
async fn get_user(
    State(state): State<AppState>,
    AuthUser(user): AuthUser,
    Path(id): Path<i64>,
) -> Result<Json<User>, AppError> {
    if user.id != id && !state.db.are_friends(user.id, id)? {
        return Err(AppError::NotFound("用户未找到".to_string()));
    }
    let user = state.db.get_user_by_id(id)?;
    Ok(Json(user))
}

/// GET /api/v1/users/{id}/public_key — 获取用户公钥
async fn get_public_key(
    State(state): State<AppState>,
    AuthUser(user): AuthUser,
    Path(id): Path<i64>,
) -> Result<Json<serde_json::Value>, AppError> {
    if user.id != id && !state.db.are_friends(user.id, id)? {
        return Err(AppError::NotFound("用户未找到".to_string()));
    }
    let pk = state.db.get_public_key(id)?;
    let encoded = base64::engine::general_purpose::STANDARD.encode(&pk);
    Ok(Json(serde_json::json!({
        "user_id": id,
        "public_key": encoded
    })))
}
