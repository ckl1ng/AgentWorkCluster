// Bearer Token 认证
// 支持两种认证方式（按优先级）：
// 1. Authorization: Bearer <token> HTTP header
// 2. ?token=xxx 查询参数（向后兼容）

use axum::{
    extract::{FromRequestParts, State},
    http::{request::Parts, StatusCode},
    middleware::Next,
    response::{IntoResponse, Response},
    Json,
};
use std::sync::Arc;

use crate::db::Database;
use crate::models::User;

/// AuthUser 提取器——从请求扩展中获取已认证的用户
pub struct AuthUser(pub User);

#[derive(Debug)]
pub enum AuthError {
    Missing,
    Invalid,
}

impl IntoResponse for AuthError {
    fn into_response(self) -> Response {
        let (status, msg) = match self {
            AuthError::Missing => (
                StatusCode::UNAUTHORIZED,
                "缺少认证 token（请使用 Authorization: Bearer <token> 或 ?token=xxx）",
            ),
            AuthError::Invalid => (
                StatusCode::UNAUTHORIZED,
                "认证 token 无效或已过期",
            ),
        };
        (status, Json(serde_json::json!({"error": msg}))).into_response()
    }
}

/// 认证中间件：从请求中提取 token，查找用户，注入到 extensions
pub async fn auth_middleware(
    State(db): State<Arc<Database>>,
    mut req: axum::http::Request<axum::body::Body>,
    next: Next,
) -> Result<Response, AuthError> {
    // Extract token from header and query directly from the request
    let token = {
        let headers = req.headers();
        let query = req.uri().query();
        let mut token = None;

        // Bearer header
        if let Some(auth) = headers.get(axum::http::header::AUTHORIZATION) {
            if let Ok(auth_str) = auth.to_str() {
                if let Some(t) = auth_str.strip_prefix("Bearer ") {
                    if !t.trim().is_empty() {
                        token = Some(t.trim().to_string());
                    }
                }
            }
        }

        // Query param fallback
        if token.is_none() {
            if let Some(query_str) = query {
                for pair in query_str.split('&') {
                    let mut kv = pair.splitn(2, '=');
                    if let (Some(key), Some(value)) = (kv.next(), kv.next()) {
                        if key == "token" && !value.is_empty() {
                            token = Some(value.to_string());
                            break;
                        }
                    }
                }
            }
        }

        token.ok_or(AuthError::Missing)?
    };

    let user = db.authenticate(&token).map_err(|_| AuthError::Invalid)?;
    req.extensions_mut().insert(user);
    Ok(next.run(req).await)
}

/// 从扩展中提取已认证用户
#[axum::async_trait]
impl<S> FromRequestParts<S> for AuthUser
where
    S: Send + Sync,
{
    type Rejection = AuthError;

    async fn from_request_parts(parts: &mut Parts, _state: &S) -> Result<Self, Self::Rejection> {
        parts
            .extensions
            .get::<User>()
            .cloned()
            .map(AuthUser)
            .ok_or(AuthError::Missing)
    }
}
