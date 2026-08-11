mod auth;
mod config;
mod crypto;
mod db;
mod error;
mod models;
mod ratelimit;
mod routes;
mod ws;

use std::sync::Arc;

use axum::{middleware, Router};
use tower_http::cors::CorsLayer;

#[tokio::main]
async fn main() {
    // 初始化 tracing — 输出到 stdout（远程 SSH 下 stderr 可能不可见）
    tracing_subscriber::fmt()
        .with_writer(std::io::stdout)
        .init();

    // 初始化加密库（用于随机 token 生成）
    crypto::init();

    // 加载配置
    let cfg = config::Config::from_env();
    tracing::info!("数据库路径: {:?}", cfg.db_path);

    // 打开数据库
    let database = db::Database::open(&cfg.db_path).expect("数据库初始化失败");
    let db = Arc::new(database);

    // 创建连接管理器
    let manager = ws::manager::ConnectionManager::new();

    // ---- 构建路由 ----

    // 共享 AppState
    let rest_state = routes::AppState { db: db.clone() };

    // 需要认证的 REST API 路由（应用 auth 中间件）
    let authed_rest = Router::new()
        .merge(routes::users::authed_routes())
        .merge(routes::groups::authed_routes())
        .merge(routes::messages::authed_routes())
        .layer(middleware::from_fn_with_state(
            db.clone(),
            auth::auth_middleware,
        ))
        .layer(axum::middleware::from_fn(ratelimit::rest_rate_limit))
        .with_state(rest_state.clone());

    // 公开路由（无需认证）
    let public_rest = Router::new()
        .route("/healthz", axum::routing::get(|| async { "ok" }))
        .merge(routes::users::public_routes(rest_state.clone()));

    let rest_app = public_rest.merge(authed_rest);

    // WebSocket 路由（在自己的 handler 中认证）
    let ws_state = Arc::new(ws::handler::AppState {
        db: db.clone(),
        manager: manager.clone(),
    });
    let ws_route = Router::new()
        .route("/ws", axum::routing::get(ws::handler::ws_handler))
        .with_state(ws_state);

    // 合并所有路由
    let mut app = Router::new()
        .merge(rest_app)
        .merge(ws_route)
        .layer(CorsLayer::permissive());

    if let Some(secret) = cfg.agent_service_secret.clone() {
        app = app.merge(routes::internal::routes(db.clone(), secret));
        tracing::info!("Agent 内部认证检查端点已启用");
    } else {
        tracing::warn!("未配置 AGENT_SERVICE_SECRET，Agent 内部认证检查端点未启用");
    }

    // 启动
    let addr = format!("{}:{}", cfg.host, cfg.port);
    eprintln!("══════════════════════════════════════════");
    eprintln!("  Chat Server v{}", env!("CARGO_PKG_VERSION"));
    eprintln!("  HTTP:      http://{}", addr);
    eprintln!("  WebSocket: ws://{}/ws", addr);
    eprintln!("  数据库:    {}", cfg.db_path.display());
    eprintln!("══════════════════════════════════════════");
    tracing::info!("聊天服务启动于 http://{}", addr);
    tracing::info!("WebSocket 端点: ws://{}/ws", addr);

    let listener = tokio::net::TcpListener::bind(&addr)
        .await
        .expect("绑定端口失败");

    tracing::info!("服务器已启动，按 Ctrl+C 优雅关闭");

    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await
        .expect("服务运行失败");

    tracing::info!("服务器已关闭");
}

/// 优雅关闭信号
async fn shutdown_signal() {
    let ctrl_c = async {
        tokio::signal::ctrl_c()
            .await
            .expect("无法安装 Ctrl+C 处理器");
    };

    #[cfg(unix)]
    let terminate = async {
        tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
            .expect("无法安装 SIGTERM 处理器")
            .recv()
            .await;
    };

    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();

    tokio::select! {
        _ = ctrl_c => {
            tracing::info!("收到 SIGINT (Ctrl+C)，正在优雅关闭...");
        },
        _ = terminate => {
            tracing::info!("收到 SIGTERM，正在优雅关闭...");
        },
    }
}
