use base64::Engine;
use serde::{Deserialize, Serialize};

// ==================== 用户 ====================

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct User {
    pub id: i64,
    pub username: String,
    #[serde(
        serialize_with = "base64_serialize",
        deserialize_with = "base64_deserialize"
    )]
    pub public_key: Vec<u8>,
    #[serde(default)]
    pub avatar: String,
    pub created_at: String,
}

#[derive(Debug, Deserialize)]
pub struct RegisterRequest {
    pub username: String,
    pub password: String,
    #[serde(deserialize_with = "base64_deserialize")]
    pub public_key: Vec<u8>,
    pub encrypted_secret_key: String,
}

#[derive(Debug, Serialize)]
pub struct RegisterResponse {
    pub id: i64,
    pub username: String,
    pub token: String,
}

#[derive(Debug, Serialize)]
pub struct LoginResponse {
    pub id: i64,
    pub username: String,
    pub token: String,
}

#[derive(Debug, Serialize)]
pub struct UserSearchResult {
    pub id: i64,
    pub username: String,
}

#[derive(Debug, Deserialize)]
pub struct FriendRequestBody {
    pub user_id: i64,
}

#[derive(Debug, Deserialize)]
pub struct UpdateAvatarRequest {
    pub avatar: String,
}

#[derive(Debug, Deserialize)]
pub struct LoginRequest {
    pub username: String,
    pub password: String,
}

// ==================== 私聊消息 ====================

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PrivateMessage {
    pub id: i64,
    pub sender_id: i64,
    pub recipient_id: i64,
    #[serde(
        serialize_with = "base64_serialize",
        deserialize_with = "base64_deserialize"
    )]
    pub encrypted_content: Vec<u8>,
    #[serde(default = "default_content_type")]
    pub content_type: String,
    pub created_at: String,
}

// ==================== 群组 ====================

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Group {
    pub id: i64,
    pub name: String,
    pub creator_id: i64,
    pub created_at: String,
}

#[derive(Debug, Deserialize)]
pub struct CreateGroupRequest {
    pub name: String,
    /// 每个成员加密后的群密钥 (base64), 顺序与 member_ids 对应
    pub member_ids: Vec<i64>,
    pub encrypted_group_keys: Vec<String>, // base64 encoded per-member
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GroupMember {
    pub user_id: i64,
    pub username: String,
    #[serde(default)]
    pub avatar: String,
    #[serde(
        serialize_with = "base64_serialize",
        deserialize_with = "base64_deserialize"
    )]
    pub encrypted_key: Vec<u8>,
}

#[derive(Debug, Deserialize)]
pub struct JoinGroupRequest {
    #[serde(deserialize_with = "base64_deserialize")]
    pub encrypted_key: Vec<u8>,
}

#[derive(Debug, Deserialize)]
pub struct AddGroupMemberRequest {
    pub user_id: i64,
    #[serde(deserialize_with = "base64_deserialize")]
    pub encrypted_key: Vec<u8>,
}

// ==================== 群消息 ====================

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GroupMessage {
    pub id: i64,
    pub group_id: i64,
    pub sender_id: i64,
    #[serde(
        serialize_with = "base64_serialize",
        deserialize_with = "base64_deserialize"
    )]
    pub encrypted_content: Vec<u8>,
    #[serde(default = "default_content_type")]
    pub content_type: String,
    pub created_at: String,
}

// ==================== WebSocket 消息类型 ====================

#[derive(Debug, Deserialize)]
pub struct WsIncoming {
    #[serde(rename = "type")]
    pub msg_type: String,
    pub to_user_id: Option<i64>,
    pub group_id: Option<i64>,
    #[serde(default, deserialize_with = "base64_deserialize_opt")]
    pub encrypted_content: Option<Vec<u8>>,
    pub content_type: Option<String>,
    pub created_at: Option<String>,
    pub client_message_id: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct WsOutgoing {
    #[serde(rename = "type")]
    pub msg_type: String,
    pub message_id: Option<i64>,
    pub from_user_id: Option<i64>,
    pub from_username: Option<String>,
    pub from_avatar: Option<String>,
    pub group_id: Option<i64>,
    #[serde(serialize_with = "base64_serialize_opt")]
    pub encrypted_content: Option<Vec<u8>>,
    pub content_type: Option<String>,
    pub created_at: Option<String>,
    pub client_message_id: Option<String>,
}

fn default_content_type() -> String {
    "text/plain".to_string()
}

// ==================== 序列化辅助 ====================

fn base64_serialize<S>(data: &[u8], serializer: S) -> Result<S::Ok, S::Error>
where
    S: serde::Serializer,
{
    let encoded = base64::engine::general_purpose::STANDARD.encode(data);
    serializer.serialize_str(&encoded)
}

fn base64_deserialize<'de, D>(deserializer: D) -> Result<Vec<u8>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    let s = String::deserialize(deserializer)?;
    base64::engine::general_purpose::STANDARD
        .decode(&s)
        .map_err(serde::de::Error::custom)
}

fn base64_serialize_opt<S>(data: &Option<Vec<u8>>, serializer: S) -> Result<S::Ok, S::Error>
where
    S: serde::Serializer,
{
    match data {
        Some(v) => base64_serialize(v, serializer),
        None => serializer.serialize_none(),
    }
}

fn base64_deserialize_opt<'de, D>(deserializer: D) -> Result<Option<Vec<u8>>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    let s: Option<String> = Option::deserialize(deserializer)?;
    match s {
        Some(s) => base64::engine::general_purpose::STANDARD
            .decode(&s)
            .map(Some)
            .map_err(serde::de::Error::custom),
        None => Ok(None),
    }
}
