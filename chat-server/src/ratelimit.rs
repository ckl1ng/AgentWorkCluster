// 简易速率限制中间件
// 按 token 限流：每个 token 100 请求/分钟（REST）或 30 消息/分钟（WebSocket）

use axum::{
    extract::Request,
    http::StatusCode,
    middleware::Next,
    response::{IntoResponse, Response},
    Json,
};
use std::{
    collections::HashMap,
    sync::Mutex,
    time::{Duration, Instant},
};
use std::sync::LazyLock;

const REST_WINDOW: Duration = Duration::from_secs(60);
const REST_MAX: u32 = 100;

static RATE_STORE: LazyLock<Mutex<HashMap<String, (Instant, u32)>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

fn extract_token_for_ratelimit(req: &Request) -> Option<String> {
    // Bearer header
    if let Some(auth) = req.headers().get(axum::http::header::AUTHORIZATION) {
        if let Ok(auth_str) = auth.to_str() {
            if let Some(token) = auth_str.strip_prefix("Bearer ") {
                return Some(token.trim().to_string());
            }
        }
    }
    // Query param
    if let Some(query) = req.uri().query() {
        for pair in query.split('&') {
            let mut kv = pair.splitn(2, '=');
            if let (Some(key), Some(value)) = (kv.next(), kv.next()) {
                if key == "token" && !value.is_empty() {
                    return Some(value.to_string());
                }
            }
        }
    }
    None
}

/// REST API 速率限制中间件
pub async fn rest_rate_limit(req: Request, next: Next) -> Result<Response, Response> {
    let token = match extract_token_for_ratelimit(&req) {
        Some(t) => t,
        None => return Ok(next.run(req).await),
    };

    {
        let mut store = RATE_STORE.lock().unwrap();
        let (window_start, count) = store.entry(token.clone()).or_insert((Instant::now(), 0));

        if window_start.elapsed() >= REST_WINDOW {
            // 重置窗口
            *window_start = Instant::now();
            *count = 0;
        }

        if *count >= REST_MAX {
            return Err((
                StatusCode::TOO_MANY_REQUESTS,
                Json(serde_json::json!({
                    "error": "请求频率过高，请稍后重试"
                })),
            )
                .into_response());
        }

        *count += 1;
    }

    Ok(next.run(req).await)
}
