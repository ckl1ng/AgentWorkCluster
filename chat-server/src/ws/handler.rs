use std::sync::Arc;

use axum::extract::{
    ws::{Message, WebSocket},
    State, WebSocketUpgrade,
};
use axum::http::header;
use axum::response::IntoResponse;
use futures::{SinkExt, StreamExt};

use crate::db::Database;
use crate::error::AppError;
use crate::models::*;
use crate::ws::manager::{ConnectionManager, DeliveryMessage};

const MAX_ENCRYPTED_CONTENT_BYTES: usize = 10 * 1024 * 1024 + 8192;
const MAX_WS_MESSAGE_BYTES: usize = 15 * 1024 * 1024;

fn validate_message_content(content_type: &str, encrypted: &[u8]) -> Result<(), &'static str> {
    if !matches!(
        content_type,
        "text/plain"
            | "image/png"
            | "image/jpeg"
            | "image/gif"
            | "image/webp"
            | "application/octet-stream"
    ) {
        return Err("不支持的消息类型");
    }
    if encrypted.len() > MAX_ENCRYPTED_CONTENT_BYTES {
        return Err("消息内容不能超过 10 MiB");
    }
    Ok(())
}

pub async fn ws_handler(
    ws: WebSocketUpgrade,
    axum::extract::Query(query): axum::extract::Query<std::collections::HashMap<String, String>>,
    headers: axum::http::HeaderMap,
    State(state): State<Arc<AppState>>,
) -> Result<impl IntoResponse, AppError> {
    // 优先从 Bearer header 提取 token，回退到 query 参数
    let token = headers
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .map(|t| t.trim().to_string())
        .or_else(|| query.get("token").cloned())
        .ok_or_else(|| AppError::BadRequest("缺少认证 token".to_string()))?;

    let user = state.db.authenticate(&token)?;
    Ok(ws
        .max_message_size(MAX_WS_MESSAGE_BYTES)
        .max_frame_size(MAX_WS_MESSAGE_BYTES)
        .on_upgrade(move |socket| handle_socket(socket, user, state)))
}

#[derive(Clone)]
pub struct AppState {
    pub db: Arc<Database>,
    pub manager: Arc<ConnectionManager>,
}

async fn handle_socket(socket: WebSocket, user: crate::models::User, state: Arc<AppState>) {
    let (mut ws_sender, mut ws_receiver) = socket.split();

    // 获取用户的消息接收通道
    let (mut delivery_rx, mut presence_rx, became_online) =
        state.manager.user_online(user.id).await;

    // 发送 connected 消息
    let connected = serde_json::json!({
        "type": "connected",
        "user_id": user.id,
        "username": user.username,
    });
    let _ = ws_sender.send(Message::Text(connected.to_string())).await;
    for online_user_id in state.manager.online_users().await {
        if online_user_id != user.id {
            let event = serde_json::json!({"type": "user_online", "user_id": online_user_id});
            let _ = ws_sender.send(Message::Text(event.to_string())).await;
        }
    }
    if became_online {
        state.manager.publish_presence(user.id, true);
    }

    // 双工处理：一个任务处理接收的消息，一个任务处理广播过来的消息
    let ws_sender = Arc::new(tokio::sync::Mutex::new(ws_sender));

    // 任务1: 从 WebSocket 读取消息
    let state_clone = state.clone();
    let sender1 = ws_sender.clone();
    let user_id = user.id;
    let mut recv_task = tokio::spawn(async move {
        while let Some(msg) = ws_receiver.next().await {
            match msg {
                Ok(Message::Text(text)) => {
                    // 解析 JSON
                    let incoming: WsIncoming = match serde_json::from_str(&text) {
                        Ok(m) => m,
                        Err(e) => {
                            let err = serde_json::json!({
                                "type": "error",
                                "message": format!("JSON 解析失败: {}", e)
                            });
                            let _ = sender1
                                .lock()
                                .await
                                .send(Message::Text(err.to_string()))
                                .await;
                            continue;
                        }
                    };
                    // 处理消息
                    process_incoming(incoming, user_id, &state_clone, &sender1).await;
                }
                Ok(Message::Close(_)) => break,
                Err(e) => {
                    eprintln!("WebSocket 接收错误: {}", e);
                    break;
                }
                _ => {}
            }
        }
    });

    // 任务2: 从广播通道接收消息并转发给 WebSocket
    let sender2 = ws_sender.clone();
    let mut send_task = tokio::spawn(async move {
        loop {
            tokio::select! {
                delivery = delivery_rx.recv() => match delivery {
                    Ok(delivery) => {
                    let outgoing = WsOutgoing {
                        msg_type: delivery.msg_type.clone(),
                        message_id: Some(delivery.message_id),
                        from_user_id: Some(delivery.from_user_id),
                        from_username: Some(delivery.from_username),
                        from_avatar: Some(delivery.from_avatar),
                        group_id: delivery.group_id,
                        encrypted_content: Some(delivery.encrypted_content),
                        content_type: Some(delivery.content_type),
                        created_at: Some(delivery.created_at),
                        client_message_id: delivery.client_message_id,
                    };
                    let json = serde_json::to_string(&outgoing).unwrap();
                    let _ = sender2.lock().await.send(Message::Text(json.into())).await;
                    }
                    Err(tokio::sync::broadcast::error::RecvError::Closed) => break,
                    Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => continue,
                },
                presence = presence_rx.recv() => match presence {
                    Ok(event) => {
                        let msg_type = if event.online { "user_online" } else { "user_offline" };
                        let json = serde_json::json!({"type": msg_type, "user_id": event.user_id});
                        let _ = sender2.lock().await.send(Message::Text(json.to_string())).await;
                    }
                    Err(tokio::sync::broadcast::error::RecvError::Closed) => break,
                    Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => continue,
                },
            }
        }
    });

    // 等待任意一个任务结束
    tokio::select! {
        _ = &mut recv_task => {},
        _ = &mut send_task => {},
    }
    // Dropping a JoinHandle detaches the task. Abort the sibling explicitly so
    // a disconnected socket cannot leave a delivery loop behind indefinitely.
    recv_task.abort();
    send_task.abort();
    let _ = recv_task.await;
    let _ = send_task.await;

    // 用户下线
    if state.manager.user_offline(user_id).await {
        state.manager.publish_presence(user_id, false);
    }
}

/// 处理收到的消息（私聊或群聊）
async fn process_incoming(
    msg: WsIncoming,
    sender_id: i64,
    state: &Arc<AppState>,
    ws_sender: &Arc<tokio::sync::Mutex<futures::stream::SplitSink<WebSocket, Message>>>,
) {
    let created_at = msg.created_at.unwrap_or_else(timestamp_now);
    let client_message_id = msg.client_message_id.clone();

    match msg.msg_type.as_str() {
        "private" => {
            let encrypted = match msg.encrypted_content {
                Some(c) => c,
                None => {
                    let err =
                        serde_json::json!({"type": "error", "message": "缺少 encrypted_content"});
                    let _ = ws_sender
                        .lock()
                        .await
                        .send(Message::Text(err.to_string()))
                        .await;
                    return;
                }
            };
            let recipient_id = match msg.to_user_id {
                Some(id) => id,
                None => {
                    let err =
                        serde_json::json!({"type": "error", "message": "私聊需要 to_user_id"});
                    let _ = ws_sender
                        .lock()
                        .await
                        .send(Message::Text(err.to_string()))
                        .await;
                    return;
                }
            };
            let content_type = msg.content_type.as_deref().unwrap_or("text/plain");
            if let Err(message) = validate_message_content(content_type, &encrypted) {
                let err = serde_json::json!({"type": "error", "message": message});
                let _ = ws_sender
                    .lock()
                    .await
                    .send(Message::Text(err.to_string()))
                    .await;
                return;
            }
            if recipient_id == sender_id
                || !state
                    .db
                    .are_friends(sender_id, recipient_id)
                    .unwrap_or(false)
            {
                let err = serde_json::json!({"type": "error", "message": "只能向好友发送私聊消息"});
                let _ = ws_sender
                    .lock()
                    .await
                    .send(Message::Text(err.to_string()))
                    .await;
                return;
            }

            // 保存到数据库
            let msg_id = match state.db.save_private_message(
                sender_id,
                recipient_id,
                &encrypted,
                content_type,
                &created_at,
            ) {
                Ok(id) => id,
                Err(e) => {
                    let err =
                        serde_json::json!({"type": "error", "message": format!("保存失败: {}", e)});
                    let _ = ws_sender
                        .lock()
                        .await
                        .send(Message::Text(err.to_string()))
                        .await;
                    return;
                }
            };

            // 获取发送者用户名
            let sender = state
                .db
                .get_user_by_id(sender_id)
                .ok();
            let sender_name = sender
                .as_ref()
                .map(|user| user.username.clone())
                .unwrap_or_else(|| format!("{}", sender_id));
            let sender_avatar = sender.map(|user| user.avatar).unwrap_or_default();

            let delivery = DeliveryMessage {
                msg_type: "private".to_string(),
                message_id: msg_id,
                from_user_id: sender_id,
                from_username: sender_name,
                from_avatar: sender_avatar,
                group_id: None,
                encrypted_content: encrypted.clone(),
                content_type: content_type.to_string(),
                created_at: created_at.clone(),
                client_message_id: client_message_id.clone(),
            };

            // 发送给接收者（在线则实时推送）
            let delivered = state.manager.send_to_user(recipient_id, delivery).await;

            // 发送 ack 给发送者
            let ack = serde_json::json!({
                "type": "ack",
                "message_id": msg_id,
                "to_user_id": recipient_id,
                "delivered": delivered,
                "created_at": created_at,
                "client_message_id": client_message_id,
            });
            let _ = ws_sender
                .lock()
                .await
                .send(Message::Text(ack.to_string()))
                .await;
        }

        "group" => {
            let encrypted = match msg.encrypted_content {
                Some(c) => c,
                None => {
                    let err =
                        serde_json::json!({"type": "error", "message": "缺少 encrypted_content"});
                    let _ = ws_sender
                        .lock()
                        .await
                        .send(Message::Text(err.to_string()))
                        .await;
                    return;
                }
            };
            let group_id = match msg.group_id {
                Some(id) => id,
                None => {
                    let err = serde_json::json!({"type": "error", "message": "群聊需要 group_id"});
                    let _ = ws_sender
                        .lock()
                        .await
                        .send(Message::Text(err.to_string()))
                        .await;
                    return;
                }
            };
            let content_type = msg.content_type.as_deref().unwrap_or("text/plain");
            if let Err(message) = validate_message_content(content_type, &encrypted) {
                let err = serde_json::json!({"type": "error", "message": message});
                let _ = ws_sender
                    .lock()
                    .await
                    .send(Message::Text(err.to_string()))
                    .await;
                return;
            }
            if !state
                .db
                .is_group_member(group_id, sender_id)
                .unwrap_or(false)
            {
                let err = serde_json::json!({"type": "error", "message": "你不是该群组成员"});
                let _ = ws_sender
                    .lock()
                    .await
                    .send(Message::Text(err.to_string()))
                    .await;
                return;
            }

            // 保存到数据库
            let msg_id = match state.db.save_group_message(
                group_id,
                sender_id,
                &encrypted,
                content_type,
                &created_at,
            ) {
                Ok(id) => id,
                Err(e) => {
                    let err =
                        serde_json::json!({"type": "error", "message": format!("保存失败: {}", e)});
                    let _ = ws_sender
                        .lock()
                        .await
                        .send(Message::Text(err.to_string()))
                        .await;
                    return;
                }
            };

            // 获取群成员列表
            let members = match state.db.get_group_members(group_id) {
                Ok(m) => m,
                Err(_) => return,
            };
            let member_ids: Vec<i64> = members.iter().map(|m| m.user_id).collect();

            // 获取发送者用户名
            let sender = state
                .db
                .get_user_by_id(sender_id)
                .ok();
            let sender_name = sender
                .as_ref()
                .map(|user| user.username.clone())
                .unwrap_or_else(|| format!("{}", sender_id));
            let sender_avatar = sender.map(|user| user.avatar).unwrap_or_default();

            let delivery = DeliveryMessage {
                msg_type: "group".to_string(),
                message_id: msg_id,
                from_user_id: sender_id,
                from_username: sender_name,
                from_avatar: sender_avatar,
                group_id: Some(group_id),
                encrypted_content: encrypted.clone(),
                content_type: content_type.to_string(),
                created_at: created_at.clone(),
                client_message_id,
            };

            // 广播给所有群成员（包括发送者，客户端自行过滤）
            state.manager.send_to_group(delivery, &member_ids).await;
        }

        "ping" => {
            let pong = serde_json::json!({"type": "pong"});
            let _ = ws_sender
                .lock()
                .await
                .send(Message::Text(pong.to_string()))
                .await;
        }

        _ => {
            let err = serde_json::json!({
                "type": "error",
                "message": format!("未知消息类型: {}", msg.msg_type)
            });
            let _ = ws_sender
                .lock()
                .await
                .send(Message::Text(err.to_string()))
                .await;
        }
    }
}

fn timestamp_now() -> String {
    chrono::Utc::now()
        .format("%Y-%m-%dT%H:%M:%S%.3fZ")
        .to_string()
}
