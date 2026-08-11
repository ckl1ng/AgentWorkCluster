use std::sync::Arc;

use axum::{
    extract::State,
    http::{header::AUTHORIZATION, HeaderMap, StatusCode},
    routing::post,
    Json, Router,
};
use serde::{Deserialize, Serialize};

use crate::db::Database;

#[derive(Clone)]
struct InternalState {
    db: Arc<Database>,
    service_secret: String,
}

#[derive(Deserialize)]
struct IntrospectRequest {
    user_token: String,
}

#[derive(Serialize)]
struct IntrospectResponse {
    user_id: i64,
    username: String,
    active: bool,
}

/// Routes intended only for services on the private network. They are only
/// registered when AGENT_SERVICE_SECRET is configured by the deployment.
pub fn routes(db: Arc<Database>, service_secret: String) -> Router {
    Router::new()
        .route("/internal/v1/auth/introspect", post(introspect))
        .with_state(InternalState { db, service_secret })
}

async fn introspect(
    State(state): State<InternalState>,
    headers: HeaderMap,
    Json(request): Json<IntrospectRequest>,
) -> Result<Json<IntrospectResponse>, StatusCode> {
    let supplied_secret = headers
        .get(AUTHORIZATION)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.strip_prefix("Service "));

    if supplied_secret != Some(state.service_secret.as_str()) {
        return Err(StatusCode::UNAUTHORIZED);
    }

    let user = state
        .db
        .authenticate(&request.user_token)
        .map_err(|_| StatusCode::UNAUTHORIZED)?;

    Ok(Json(IntrospectResponse {
        user_id: user.id,
        username: user.username,
        active: true,
    }))
}
