use std::path::Path;
use std::sync::Mutex;

use redb::{Database as RedbDb, ReadableTable, TableDefinition};

use crate::error::AppError;
use crate::models::*;

// 表定义
const USERS: TableDefinition<u64, &str> = TableDefinition::new("users");
const USERNAME_INDEX: TableDefinition<&str, u64> = TableDefinition::new("username_index");
const TOKEN_INDEX: TableDefinition<&str, u64> = TableDefinition::new("token_index");
const USER_PASSWORDS: TableDefinition<u64, &str> = TableDefinition::new("user_passwords");
const USER_KEY_BACKUPS: TableDefinition<u64, &str> = TableDefinition::new("user_key_backups");
const FRIENDS: TableDefinition<(u64, u64), ()> = TableDefinition::new("friends");
const FRIEND_REQUESTS: TableDefinition<(u64, u64), ()> = TableDefinition::new("friend_requests");

// 私聊: key = (conversation_id << 32 | seq_id) as u128, value = JSON
// conversation_id = min(user_a, user_b) << 32 | max(user_a, user_b)
const PRIVATE_MSGS: TableDefinition<u128, &str> = TableDefinition::new("private_msgs");
const PRIVATE_SEQ: TableDefinition<u64, u64> = TableDefinition::new("private_seq"); // conversation_id -> next_seq

// 群组
const GROUPS: TableDefinition<u64, &str> = TableDefinition::new("groups");
const GROUP_MEMBERS: TableDefinition<(u64, u64), &[u8]> = TableDefinition::new("group_members");
const GROUP_MSGS: TableDefinition<u128, &str> = TableDefinition::new("group_msgs");
const GROUP_MSG_SEQ: TableDefinition<u64, u64> = TableDefinition::new("group_msg_seq"); // group_id -> next_seq
const GROUP_NEXT_ID: TableDefinition<u64, u64> = TableDefinition::new("group_next_id");
const USER_GROUPS: TableDefinition<(u64, u64), ()> = TableDefinition::new("user_groups"); // (user_id, group_id) 反向索引

// 元数据
const NEXT_USER_ID: TableDefinition<u64, u64> = TableDefinition::new("next_user_id");

pub struct Database {
    db: Mutex<RedbDb>,
}

impl Database {
    pub fn open(path: &Path) -> Result<Self, AppError> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)
                .map_err(|e| AppError::Internal(format!("无法创建数据目录: {}", e)))?;
        }

        let db = RedbDb::create(path)
            .map_err(|e| AppError::Database(format!("打开数据库失败: {}", e)))?;

        // 初始化表
        let write_txn = db
            .begin_write()
            .map_err(|e| AppError::Database(format!("事务失败: {}", e)))?;
        {
            let _ = write_txn.open_table(USERS);
            let _ = write_txn.open_table(USERNAME_INDEX);
            let _ = write_txn.open_table(TOKEN_INDEX);
            let _ = write_txn.open_table(USER_PASSWORDS);
            let _ = write_txn.open_table(USER_KEY_BACKUPS);
            let _ = write_txn.open_table(FRIENDS);
            let _ = write_txn.open_table(FRIEND_REQUESTS);
            let _ = write_txn.open_table(PRIVATE_MSGS);
            let _ = write_txn.open_table(PRIVATE_SEQ);
            let _ = write_txn.open_table(GROUPS);
            let _ = write_txn.open_table(GROUP_MEMBERS);
            let _ = write_txn.open_table(GROUP_MSGS);
            let _ = write_txn.open_table(GROUP_MSG_SEQ);
            let _ = write_txn.open_table(GROUP_NEXT_ID);
            let _ = write_txn.open_table(USER_GROUPS);
            let _ = write_txn.open_table(NEXT_USER_ID);
        }
        write_txn
            .commit()
            .map_err(|e| AppError::Database(format!("提交失败: {}", e)))?;

        Ok(Database { db: Mutex::new(db) })
    }

    // ==================== 用户 ====================

    pub fn register_user(
        &self,
        username: &str,
        public_key: &[u8],
        token: &str,
        password_hash: &str,
        encrypted_secret_key: &str,
    ) -> Result<i64, AppError> {
        let db = self.db.lock().unwrap();
        let write_txn = db
            .begin_write()
            .map_err(|e| AppError::Database(e.to_string()))?;

        // 检查用户名是否已存在
        {
            let table = write_txn
                .open_table(USERNAME_INDEX)
                .map_err(|e| AppError::Database(e.to_string()))?;
            if table
                .get(username)
                .map_err(|e| AppError::Database(e.to_string()))?
                .is_some()
            {
                return Err(AppError::BadRequest("用户名已存在".to_string()));
            }
        }

        // 获取自增 ID
        let mut next_id_table = write_txn
            .open_table(NEXT_USER_ID)
            .map_err(|e| AppError::Database(e.to_string()))?;
        let user_id = match next_id_table
            .get(0)
            .map_err(|e| AppError::Database(e.to_string()))?
        {
            Some(v) => v.value() + 1,
            None => 1,
        };
        next_id_table
            .insert(0, user_id)
            .map_err(|e| AppError::Database(e.to_string()))?;
        drop(next_id_table);

        let now = chrono::Utc::now()
            .format("%Y-%m-%dT%H:%M:%S%.3fZ")
            .to_string();

        let user = User {
            id: user_id as i64,
            username: username.to_string(),
            public_key: public_key.to_vec(),
            avatar: String::new(),
            created_at: now,
        };
        let json = serde_json::to_string(&user).map_err(|e| AppError::Internal(e.to_string()))?;

        // 写入用户表
        let mut users_table = write_txn
            .open_table(USERS)
            .map_err(|e| AppError::Database(e.to_string()))?;
        users_table
            .insert(user_id, json.as_str())
            .map_err(|e| AppError::Database(e.to_string()))?;
        drop(users_table);

        // 写入用户名索引
        let mut username_table = write_txn
            .open_table(USERNAME_INDEX)
            .map_err(|e| AppError::Database(e.to_string()))?;
        username_table
            .insert(username, user_id)
            .map_err(|e| AppError::Database(e.to_string()))?;
        drop(username_table);

        // 写入 token 索引
        let mut token_table = write_txn
            .open_table(TOKEN_INDEX)
            .map_err(|e| AppError::Database(e.to_string()))?;
        token_table
            .insert(token, user_id)
            .map_err(|e| AppError::Database(e.to_string()))?;
        drop(token_table);

        let mut passwords_table = write_txn
            .open_table(USER_PASSWORDS)
            .map_err(|e| AppError::Database(e.to_string()))?;
        passwords_table
            .insert(user_id, password_hash)
            .map_err(|e| AppError::Database(e.to_string()))?;
        drop(passwords_table);

        let mut key_backups_table = write_txn
            .open_table(USER_KEY_BACKUPS)
            .map_err(|e| AppError::Database(e.to_string()))?;
        key_backups_table
            .insert(user_id, encrypted_secret_key)
            .map_err(|e| AppError::Database(e.to_string()))?;
        drop(key_backups_table);

        write_txn
            .commit()
            .map_err(|e| AppError::Database(e.to_string()))?;

        Ok(user_id as i64)
    }

    pub fn get_user_by_token(&self, token: &str) -> Result<User, AppError> {
        let db = self.db.lock().unwrap();
        let read_txn = db
            .begin_read()
            .map_err(|e| AppError::Database(e.to_string()))?;

        let token_table = read_txn
            .open_table(TOKEN_INDEX)
            .map_err(|e| AppError::Database(e.to_string()))?;
        let user_id = token_table
            .get(token)
            .map_err(|e| AppError::Database(e.to_string()))?
            .ok_or_else(|| AppError::NotFound("用户未找到或 token 无效".to_string()))?
            .value();

        self.load_user(&read_txn, user_id)
    }

    pub fn replace_user_token(&self, user_id: i64, token: &str) -> Result<(), AppError> {
        let db = self.db.lock().unwrap();
        let write_txn = db
            .begin_write()
            .map_err(|e| AppError::Database(e.to_string()))?;
        let mut tokens = write_txn
            .open_table(TOKEN_INDEX)
            .map_err(|e| AppError::Database(e.to_string()))?;
        let stale_tokens: Vec<String> = tokens
            .iter()
            .map_err(|e| AppError::Database(e.to_string()))?
            .filter_map(|entry| {
                let (key, value) = entry.ok()?;
                (value.value() == user_id as u64).then(|| key.value().to_string())
            })
            .collect();
        for stale in stale_tokens {
            tokens
                .remove(stale.as_str())
                .map_err(|e| AppError::Database(e.to_string()))?;
        }
        tokens
            .insert(token, user_id as u64)
            .map_err(|e| AppError::Database(e.to_string()))?;
        drop(tokens);
        write_txn
            .commit()
            .map_err(|e| AppError::Database(e.to_string()))?;
        Ok(())
    }

    pub fn get_user_by_id(&self, id: i64) -> Result<User, AppError> {
        let db = self.db.lock().unwrap();
        let read_txn = db
            .begin_read()
            .map_err(|e| AppError::Database(e.to_string()))?;
        self.load_user(&read_txn, id as u64)
    }

    pub fn update_user_avatar(&self, user_id: i64, avatar: &str) -> Result<User, AppError> {
        let db = self.db.lock().unwrap();
        let write_txn = db
            .begin_write()
            .map_err(|e| AppError::Database(e.to_string()))?;
        let mut users = write_txn
            .open_table(USERS)
            .map_err(|e| AppError::Database(e.to_string()))?;
        let mut user: User = {
            let json = users
                .get(user_id as u64)
                .map_err(|e| AppError::Database(e.to_string()))?
                .ok_or_else(|| AppError::NotFound("用户未找到".to_string()))?;
            serde_json::from_str(json.value()).map_err(|e| AppError::Internal(e.to_string()))?
        };
        user.avatar = avatar.to_string();
        let updated = serde_json::to_string(&user).map_err(|e| AppError::Internal(e.to_string()))?;
        users
            .insert(user_id as u64, updated.as_str())
            .map_err(|e| AppError::Database(e.to_string()))?;
        drop(users);
        write_txn
            .commit()
            .map_err(|e| AppError::Database(e.to_string()))?;
        Ok(user)
    }

    pub fn get_user_by_username(&self, username: &str) -> Result<User, AppError> {
        let db = self.db.lock().unwrap();
        let read_txn = db
            .begin_read()
            .map_err(|e| AppError::Database(e.to_string()))?;

        let username_table = read_txn
            .open_table(USERNAME_INDEX)
            .map_err(|e| AppError::Database(e.to_string()))?;
        let user_id = username_table
            .get(username)
            .map_err(|e| AppError::Database(e.to_string()))?
            .ok_or_else(|| AppError::NotFound(format!("用户 {} 未找到", username)))?
            .value();

        self.load_user(&read_txn, user_id)
    }

    pub fn authenticate_password(&self, username: &str, password: &str) -> Result<User, AppError> {
        let db = self.db.lock().unwrap();
        let read_txn = db
            .begin_read()
            .map_err(|e| AppError::Database(e.to_string()))?;
        let username_table = read_txn
            .open_table(USERNAME_INDEX)
            .map_err(|e| AppError::Database(e.to_string()))?;
        let user_id = username_table
            .get(username)
            .map_err(|e| AppError::Database(e.to_string()))?
            .ok_or_else(|| AppError::Unauthorized("用户名或密码错误".to_string()))?
            .value();
        let passwords_table = read_txn
            .open_table(USER_PASSWORDS)
            .map_err(|e| AppError::Database(e.to_string()))?;
        let password_hash = passwords_table
            .get(user_id)
            .map_err(|e| AppError::Database(e.to_string()))?
            .ok_or_else(|| {
                AppError::Unauthorized("该账户未设置密码，请使用已有 token 登录".to_string())
            })?;
        if !bcrypt::verify(password, password_hash.value())
            .map_err(|_| AppError::Unauthorized("用户名或密码错误".to_string()))?
        {
            return Err(AppError::Unauthorized("用户名或密码错误".to_string()));
        }
        self.load_user(&read_txn, user_id)
    }

    pub fn get_encrypted_secret_key(&self, user_id: i64) -> Result<Option<String>, AppError> {
        let db = self.db.lock().unwrap();
        let read_txn = db
            .begin_read()
            .map_err(|e| AppError::Database(e.to_string()))?;
        let key_backups = read_txn
            .open_table(USER_KEY_BACKUPS)
            .map_err(|e| AppError::Database(e.to_string()))?;
        Ok(key_backups
            .get(user_id as u64)
            .map_err(|e| AppError::Database(e.to_string()))?
            .map(|value| value.value().to_string()))
    }

    pub fn save_encrypted_secret_key(
        &self,
        user_id: i64,
        encrypted_secret_key: &str,
    ) -> Result<(), AppError> {
        let db = self.db.lock().unwrap();
        let write_txn = db
            .begin_write()
            .map_err(|e| AppError::Database(e.to_string()))?;
        let mut key_backups = write_txn
            .open_table(USER_KEY_BACKUPS)
            .map_err(|e| AppError::Database(e.to_string()))?;
        key_backups
            .insert(user_id as u64, encrypted_secret_key)
            .map_err(|e| AppError::Database(e.to_string()))?;
        drop(key_backups);
        write_txn
            .commit()
            .map_err(|e| AppError::Database(e.to_string()))
    }

    pub fn list_users(&self) -> Result<Vec<User>, AppError> {
        let db = self.db.lock().unwrap();
        let read_txn = db
            .begin_read()
            .map_err(|e| AppError::Database(e.to_string()))?;

        let users_table = read_txn
            .open_table(USERS)
            .map_err(|e| AppError::Database(e.to_string()))?;
        let mut users = Vec::new();
        for result in users_table
            .iter()
            .map_err(|e| AppError::Database(e.to_string()))?
        {
            let (_key, val) = result.map_err(|e| AppError::Database(e.to_string()))?;
            let user: User =
                serde_json::from_str(val.value()).map_err(|e| AppError::Internal(e.to_string()))?;
            users.push(user);
        }
        Ok(users)
    }

    pub fn search_users(&self, query: &str, excluded_user_id: i64) -> Result<Vec<User>, AppError> {
        let query = query.to_lowercase();
        let users = self.list_users()?;
        Ok(users
            .into_iter()
            .filter(|user| {
                user.id != excluded_user_id && user.username.to_lowercase().contains(&query)
            })
            .take(20)
            .collect())
    }

    pub fn are_friends(&self, user_id: i64, other_id: i64) -> Result<bool, AppError> {
        let db = self.db.lock().unwrap();
        let read_txn = db
            .begin_read()
            .map_err(|e| AppError::Database(e.to_string()))?;
        let friends = read_txn
            .open_table(FRIENDS)
            .map_err(|e| AppError::Database(e.to_string()))?;
        Ok(friends
            .get((user_id as u64, other_id as u64))
            .map_err(|e| AppError::Database(e.to_string()))?
            .is_some())
    }

    pub fn list_friends(&self, user_id: i64) -> Result<Vec<User>, AppError> {
        let db = self.db.lock().unwrap();
        let read_txn = db
            .begin_read()
            .map_err(|e| AppError::Database(e.to_string()))?;
        let friends = read_txn
            .open_table(FRIENDS)
            .map_err(|e| AppError::Database(e.to_string()))?;
        let ids: Vec<u64> = friends
            .range((user_id as u64, 0)..=(user_id as u64, u64::MAX))
            .map_err(|e| AppError::Database(e.to_string()))?
            .filter_map(|entry| entry.ok().map(|(key, _)| key.value().1))
            .collect();
        ids.into_iter()
            .map(|id| self.load_user(&read_txn, id))
            .collect()
    }

    pub fn create_friend_request(&self, sender_id: i64, recipient_id: i64) -> Result<(), AppError> {
        if sender_id == recipient_id {
            return Err(AppError::BadRequest("不能添加自己为好友".to_string()));
        }
        self.get_user_by_id(recipient_id)?;
        if self.are_friends(sender_id, recipient_id)? {
            return Err(AppError::BadRequest("对方已经是你的好友".to_string()));
        }
        let db = self.db.lock().unwrap();
        let write_txn = db
            .begin_write()
            .map_err(|e| AppError::Database(e.to_string()))?;
        let mut requests = write_txn
            .open_table(FRIEND_REQUESTS)
            .map_err(|e| AppError::Database(e.to_string()))?;
        if requests
            .get((recipient_id as u64, sender_id as u64))
            .map_err(|e| AppError::Database(e.to_string()))?
            .is_some()
        {
            return Err(AppError::BadRequest("已发送好友请求".to_string()));
        }
        requests
            .insert((recipient_id as u64, sender_id as u64), ())
            .map_err(|e| AppError::Database(e.to_string()))?;
        drop(requests);
        write_txn
            .commit()
            .map_err(|e| AppError::Database(e.to_string()))?;
        Ok(())
    }

    pub fn list_friend_requests(&self, recipient_id: i64) -> Result<Vec<User>, AppError> {
        let db = self.db.lock().unwrap();
        let read_txn = db
            .begin_read()
            .map_err(|e| AppError::Database(e.to_string()))?;
        let requests = read_txn
            .open_table(FRIEND_REQUESTS)
            .map_err(|e| AppError::Database(e.to_string()))?;
        let sender_ids: Vec<u64> = requests
            .range((recipient_id as u64, 0)..=(recipient_id as u64, u64::MAX))
            .map_err(|e| AppError::Database(e.to_string()))?
            .filter_map(|entry| entry.ok().map(|(key, _)| key.value().1))
            .collect();
        sender_ids
            .into_iter()
            .map(|id| self.load_user(&read_txn, id))
            .collect()
    }

    pub fn accept_friend_request(&self, recipient_id: i64, sender_id: i64) -> Result<(), AppError> {
        let db = self.db.lock().unwrap();
        let write_txn = db
            .begin_write()
            .map_err(|e| AppError::Database(e.to_string()))?;
        let mut requests = write_txn
            .open_table(FRIEND_REQUESTS)
            .map_err(|e| AppError::Database(e.to_string()))?;
        if requests
            .remove((recipient_id as u64, sender_id as u64))
            .map_err(|e| AppError::Database(e.to_string()))?
            .is_none()
        {
            return Err(AppError::NotFound("好友请求不存在".to_string()));
        }
        drop(requests);
        let mut friends = write_txn
            .open_table(FRIENDS)
            .map_err(|e| AppError::Database(e.to_string()))?;
        friends
            .insert((recipient_id as u64, sender_id as u64), ())
            .map_err(|e| AppError::Database(e.to_string()))?;
        friends
            .insert((sender_id as u64, recipient_id as u64), ())
            .map_err(|e| AppError::Database(e.to_string()))?;
        drop(friends);
        write_txn
            .commit()
            .map_err(|e| AppError::Database(e.to_string()))?;
        Ok(())
    }

    pub fn get_public_key(&self, user_id: i64) -> Result<Vec<u8>, AppError> {
        let user = self.get_user_by_id(user_id)?;
        Ok(user.public_key)
    }

    fn load_user(&self, read_txn: &redb::ReadTransaction, user_id: u64) -> Result<User, AppError> {
        let users_table = read_txn
            .open_table(USERS)
            .map_err(|e| AppError::Database(e.to_string()))?;
        let json = users_table
            .get(user_id)
            .map_err(|e| AppError::Database(e.to_string()))?
            .ok_or_else(|| AppError::NotFound(format!("用户 {} 未找到", user_id)))?;
        let user: User =
            serde_json::from_str(json.value()).map_err(|e| AppError::Internal(e.to_string()))?;
        Ok(user)
    }

    // ==================== 私聊消息 ====================

    pub fn save_private_message(
        &self,
        sender_id: i64,
        recipient_id: i64,
        encrypted_content: &[u8],
        content_type: &str,
        created_at: &str,
    ) -> Result<i64, AppError> {
        let db = self.db.lock().unwrap();
        let write_txn = db
            .begin_write()
            .map_err(|e| AppError::Database(e.to_string()))?;

        let conv_id = conversation_key(sender_id as u64, recipient_id as u64);

        // 获取下一个 seq
        let mut seq_table = write_txn
            .open_table(PRIVATE_SEQ)
            .map_err(|e| AppError::Database(e.to_string()))?;
        let seq = match seq_table
            .get(conv_id)
            .map_err(|e| AppError::Database(e.to_string()))?
        {
            Some(v) => v.value() + 1,
            None => 1,
        };
        seq_table
            .insert(conv_id, seq)
            .map_err(|e| AppError::Database(e.to_string()))?;
        drop(seq_table);

        let msg_key: u128 = (conv_id as u128) << 32 | seq as u128;

        let msg = PrivateMessage {
            id: seq as i64,
            sender_id,
            recipient_id,
            encrypted_content: encrypted_content.to_vec(),
            content_type: content_type.to_string(),
            created_at: created_at.to_string(),
        };
        let json = serde_json::to_string(&msg).map_err(|e| AppError::Internal(e.to_string()))?;

        let mut msgs_table = write_txn
            .open_table(PRIVATE_MSGS)
            .map_err(|e| AppError::Database(e.to_string()))?;
        msgs_table
            .insert(msg_key, json.as_str())
            .map_err(|e| AppError::Database(e.to_string()))?;
        drop(msgs_table);

        write_txn
            .commit()
            .map_err(|e| AppError::Database(e.to_string()))?;

        Ok(seq as i64)
    }

    pub fn get_private_messages(
        &self,
        user_id: i64,
        other_id: i64,
        limit: i64,
    ) -> Result<Vec<PrivateMessage>, AppError> {
        let db = self.db.lock().unwrap();
        let read_txn = db
            .begin_read()
            .map_err(|e| AppError::Database(e.to_string()))?;

        let conv_id = conversation_key(user_id as u64, other_id as u64);
        let start_key = (conv_id as u128) << 32 | 1;
        let end_key = ((conv_id as u128) << 32) | u64::MAX as u128;

        let msgs_table = read_txn
            .open_table(PRIVATE_MSGS)
            .map_err(|e| AppError::Database(e.to_string()))?;
        let range = msgs_table
            .range(start_key..=end_key)
            .map_err(|e| AppError::Database(e.to_string()))?;

        let mut msgs = Vec::new();
        for result in range {
            let (_key, val) = result.map_err(|e| AppError::Database(e.to_string()))?;
            let msg: PrivateMessage =
                serde_json::from_str(val.value()).map_err(|e| AppError::Internal(e.to_string()))?;
            msgs.push(msg);
            if msgs.len() >= limit as usize {
                break;
            }
        }
        Ok(msgs)
    }

    // ==================== 群组操作 ====================

    pub fn create_group(
        &self,
        name: &str,
        creator_id: i64,
        member_ids: &[i64],
        encrypted_keys: &[Vec<u8>],
    ) -> Result<i64, AppError> {
        let db = self.db.lock().unwrap();
        let write_txn = db
            .begin_write()
            .map_err(|e| AppError::Database(e.to_string()))?;

        // 获取自增 ID
        let mut next_id_table = write_txn
            .open_table(GROUP_NEXT_ID)
            .map_err(|e| AppError::Database(e.to_string()))?;
        let group_id = match next_id_table
            .get(0)
            .map_err(|e| AppError::Database(e.to_string()))?
        {
            Some(v) => v.value() + 1,
            None => 1,
        };
        next_id_table
            .insert(0, group_id)
            .map_err(|e| AppError::Database(e.to_string()))?;
        drop(next_id_table);

        let now = chrono::Utc::now()
            .format("%Y-%m-%dT%H:%M:%S%.3fZ")
            .to_string();
        let group = Group {
            id: group_id as i64,
            name: name.to_string(),
            creator_id,
            created_at: now,
        };
        let json = serde_json::to_string(&group).map_err(|e| AppError::Internal(e.to_string()))?;

        let mut groups_table = write_txn
            .open_table(GROUPS)
            .map_err(|e| AppError::Database(e.to_string()))?;
        groups_table
            .insert(group_id, json.as_str())
            .map_err(|e| AppError::Database(e.to_string()))?;
        drop(groups_table);

        let mut members_table = write_txn
            .open_table(GROUP_MEMBERS)
            .map_err(|e| AppError::Database(e.to_string()))?;
        for (i, &uid) in member_ids.iter().enumerate() {
            let key = encrypted_keys
                .get(i)
                .ok_or_else(|| AppError::BadRequest("成员密钥数量不匹配".to_string()))?;
            members_table
                .insert((group_id, uid as u64), key.as_slice())
                .map_err(|e| AppError::Database(e.to_string()))?;
        }
        drop(members_table);

        // 写入反向索引：user_id -> group_id
        {
            let mut ug_table = write_txn
                .open_table(USER_GROUPS)
                .map_err(|e| AppError::Database(e.to_string()))?;
            for &uid in member_ids.iter() {
                ug_table
                    .insert((uid as u64, group_id), ())
                    .map_err(|e| AppError::Database(e.to_string()))?;
            }
        }

        write_txn
            .commit()
            .map_err(|e| AppError::Database(e.to_string()))?;

        Ok(group_id as i64)
    }

    pub fn join_group(
        &self,
        group_id: i64,
        user_id: i64,
        encrypted_key: &[u8],
    ) -> Result<(), AppError> {
        let db = self.db.lock().unwrap();
        let write_txn = db
            .begin_write()
            .map_err(|e| AppError::Database(e.to_string()))?;

        let members_table = write_txn
            .open_table(GROUP_MEMBERS)
            .map_err(|e| AppError::Database(e.to_string()))?;
        // 检查是否已经是成员
        if members_table
            .get((group_id as u64, user_id as u64))
            .map_err(|e| AppError::Database(e.to_string()))?
            .is_some()
        {
            return Err(AppError::BadRequest("用户已是群组成员".to_string()));
        }
        drop(members_table);

        let mut members_table = write_txn
            .open_table(GROUP_MEMBERS)
            .map_err(|e| AppError::Database(e.to_string()))?;
        members_table
            .insert((group_id as u64, user_id as u64), encrypted_key)
            .map_err(|e| AppError::Database(e.to_string()))?;
        drop(members_table);

        // 写入反向索引
        {
            let mut ug_table = write_txn
                .open_table(USER_GROUPS)
                .map_err(|e| AppError::Database(e.to_string()))?;
            ug_table
                .insert((user_id as u64, group_id as u64), ())
                .map_err(|e| AppError::Database(e.to_string()))?;
        }

        write_txn
            .commit()
            .map_err(|e| AppError::Database(e.to_string()))?;
        Ok(())
    }

    pub fn get_group(&self, group_id: i64) -> Result<Group, AppError> {
        let db = self.db.lock().unwrap();
        let read_txn = db
            .begin_read()
            .map_err(|e| AppError::Database(e.to_string()))?;
        let groups = read_txn
            .open_table(GROUPS)
            .map_err(|e| AppError::Database(e.to_string()))?;
        let json = groups
            .get(group_id as u64)
            .map_err(|e| AppError::Database(e.to_string()))?
            .ok_or_else(|| AppError::NotFound("群组不存在".to_string()))?;
        serde_json::from_str(json.value()).map_err(|e| AppError::Internal(e.to_string()))
    }

    pub fn is_group_member(&self, group_id: i64, user_id: i64) -> Result<bool, AppError> {
        let db = self.db.lock().unwrap();
        let read_txn = db
            .begin_read()
            .map_err(|e| AppError::Database(e.to_string()))?;
        let members = read_txn
            .open_table(GROUP_MEMBERS)
            .map_err(|e| AppError::Database(e.to_string()))?;
        Ok(members
            .get((group_id as u64, user_id as u64))
            .map_err(|e| AppError::Database(e.to_string()))?
            .is_some())
    }

    pub fn add_group_member(
        &self,
        group_id: i64,
        user_id: i64,
        encrypted_key: &[u8],
    ) -> Result<(), AppError> {
        self.get_group(group_id)?;
        self.get_user_by_id(user_id)?;
        let db = self.db.lock().unwrap();
        let write_txn = db
            .begin_write()
            .map_err(|e| AppError::Database(e.to_string()))?;
        let mut members = write_txn
            .open_table(GROUP_MEMBERS)
            .map_err(|e| AppError::Database(e.to_string()))?;
        if members
            .get((group_id as u64, user_id as u64))
            .map_err(|e| AppError::Database(e.to_string()))?
            .is_some()
        {
            return Err(AppError::BadRequest("用户已是群组成员".to_string()));
        }
        members
            .insert((group_id as u64, user_id as u64), encrypted_key)
            .map_err(|e| AppError::Database(e.to_string()))?;
        drop(members);
        let mut user_groups = write_txn
            .open_table(USER_GROUPS)
            .map_err(|e| AppError::Database(e.to_string()))?;
        user_groups
            .insert((user_id as u64, group_id as u64), ())
            .map_err(|e| AppError::Database(e.to_string()))?;
        drop(user_groups);
        write_txn
            .commit()
            .map_err(|e| AppError::Database(e.to_string()))?;
        Ok(())
    }

    pub fn get_user_groups(&self, user_id: i64) -> Result<Vec<Group>, AppError> {
        let db = self.db.lock().unwrap();
        let read_txn = db
            .begin_read()
            .map_err(|e| AppError::Database(e.to_string()))?;

        // 使用反向索引查找用户所属群组 (O(k) 而非 O(n))
        let ug_table = read_txn
            .open_table(USER_GROUPS)
            .map_err(|e| AppError::Database(e.to_string()))?;

        let mut group_ids = Vec::new();
        // 范围扫描：查找所有以 user_id 开头的 key
        let start = (user_id as u64, 0);
        let end = (user_id as u64, u64::MAX);
        let range = ug_table
            .range(start..=end)
            .map_err(|e| AppError::Database(e.to_string()))?;
        for entry in range {
            let (key_guard, _) = entry.map_err(|e| AppError::Database(e.to_string()))?;
            group_ids.push(key_guard.value().1);
        }

        // 加载群组信息
        let groups_table = read_txn
            .open_table(GROUPS)
            .map_err(|e| AppError::Database(e.to_string()))?;
        let mut groups = Vec::new();
        for gid in group_ids {
            if let Some(json) = groups_table
                .get(gid)
                .map_err(|e| AppError::Database(e.to_string()))?
            {
                let group: Group = serde_json::from_str(json.value())
                    .map_err(|e| AppError::Internal(e.to_string()))?;
                groups.push(group);
            }
        }
        Ok(groups)
    }

    pub fn get_group_members(&self, group_id: i64) -> Result<Vec<GroupMember>, AppError> {
        let db = self.db.lock().unwrap();
        let read_txn = db
            .begin_read()
            .map_err(|e| AppError::Database(e.to_string()))?;

        let members_table = read_txn
            .open_table(GROUP_MEMBERS)
            .map_err(|e| AppError::Database(e.to_string()))?;

        let users_table = read_txn
            .open_table(USERS)
            .map_err(|e| AppError::Database(e.to_string()))?;

        let mut members = Vec::new();
        let result = members_table
            .iter()
            .map_err(|e| AppError::Database(e.to_string()))?;
        for entry in result.into_iter() {
            let (key_guard, val_guard) = entry.map_err(|e| AppError::Database(e.to_string()))?;
            let key = key_guard.value();
            if key.0 == group_id as u64 {
                let uid = key.1;
                // 获取用户名
                let user = users_table
                    .get(uid)
                    .map_err(|e| AppError::Database(e.to_string()))?
                    .and_then(|u| serde_json::from_str::<User>(u.value()).ok());
                let username = user
                    .as_ref()
                    .map(|user| user.username.clone())
                    .unwrap_or_else(|| format!("user_{}", uid));
                let avatar = user.map(|user| user.avatar).unwrap_or_default();

                members.push(GroupMember {
                    user_id: uid as i64,
                    username,
                    avatar,
                    encrypted_key: val_guard.value().to_vec(),
                });
            }
        }
        Ok(members)
    }

    // ==================== 群消息 ====================

    pub fn save_group_message(
        &self,
        group_id: i64,
        sender_id: i64,
        encrypted_content: &[u8],
        content_type: &str,
        created_at: &str,
    ) -> Result<i64, AppError> {
        let db = self.db.lock().unwrap();
        let write_txn = db
            .begin_write()
            .map_err(|e| AppError::Database(e.to_string()))?;

        let mut seq_table = write_txn
            .open_table(GROUP_MSG_SEQ)
            .map_err(|e| AppError::Database(e.to_string()))?;
        let seq = match seq_table
            .get(group_id as u64)
            .map_err(|e| AppError::Database(e.to_string()))?
        {
            Some(v) => v.value() + 1,
            None => 1,
        };
        seq_table
            .insert(group_id as u64, seq)
            .map_err(|e| AppError::Database(e.to_string()))?;
        drop(seq_table);

        let msg_key: u128 = (group_id as u128) << 32 | seq as u128;

        let msg = GroupMessage {
            id: seq as i64,
            group_id,
            sender_id,
            encrypted_content: encrypted_content.to_vec(),
            content_type: content_type.to_string(),
            created_at: created_at.to_string(),
        };
        let json = serde_json::to_string(&msg).map_err(|e| AppError::Internal(e.to_string()))?;

        let mut msgs_table = write_txn
            .open_table(GROUP_MSGS)
            .map_err(|e| AppError::Database(e.to_string()))?;
        msgs_table
            .insert(msg_key, json.as_str())
            .map_err(|e| AppError::Database(e.to_string()))?;
        drop(msgs_table);

        write_txn
            .commit()
            .map_err(|e| AppError::Database(e.to_string()))?;

        Ok(seq as i64)
    }

    pub fn get_group_messages(
        &self,
        group_id: i64,
        limit: i64,
    ) -> Result<Vec<GroupMessage>, AppError> {
        let db = self.db.lock().unwrap();
        let read_txn = db
            .begin_read()
            .map_err(|e| AppError::Database(e.to_string()))?;

        let start_key = (group_id as u128) << 32 | 1;
        let end_key = ((group_id as u128) << 32) | u64::MAX as u128;

        let msgs_table = read_txn
            .open_table(GROUP_MSGS)
            .map_err(|e| AppError::Database(e.to_string()))?;
        let range = msgs_table
            .range(start_key..=end_key)
            .map_err(|e| AppError::Database(e.to_string()))?;

        let mut msgs = Vec::new();
        for result in range {
            let (_key, val) = result.map_err(|e| AppError::Database(e.to_string()))?;
            let msg: GroupMessage =
                serde_json::from_str(val.value()).map_err(|e| AppError::Internal(e.to_string()))?;
            msgs.push(msg);
            if msgs.len() >= limit as usize {
                break;
            }
        }
        Ok(msgs)
    }

    /// 验证 token 并返回用户
    pub fn authenticate(&self, token: &str) -> Result<User, AppError> {
        self.get_user_by_token(token)
    }

    // ==================== 游标分页查询 ====================

    pub fn get_private_messages_paginated(
        &self,
        user_id: i64,
        other_id: i64,
        limit: i64,
        before_id: Option<i64>,
        after_id: Option<i64>,
    ) -> Result<Vec<PrivateMessage>, AppError> {
        let db = self.db.lock().unwrap();
        let read_txn = db
            .begin_read()
            .map_err(|e| AppError::Database(e.to_string()))?;

        let conv_id = conversation_key(user_id as u64, other_id as u64);

        let msgs_table = read_txn
            .open_table(PRIVATE_MSGS)
            .map_err(|e| AppError::Database(e.to_string()))?;

        let mut msgs = Vec::new();

        if let Some(before) = before_id {
            // 返回 before_id 之前的消息（更早的消息）
            let end_key = (conv_id as u128) << 32 | before as u128;
            let start_key = (conv_id as u128) << 32 | 1;
            let range = msgs_table
                .range(start_key..end_key)
                .map_err(|e| AppError::Database(e.to_string()))?;
            for result in range.rev() {
                let (_key, val) = result.map_err(|e| AppError::Database(e.to_string()))?;
                let msg: PrivateMessage = serde_json::from_str(val.value())
                    .map_err(|e| AppError::Internal(e.to_string()))?;
                msgs.push(msg);
                if msgs.len() >= limit as usize {
                    break;
                }
            }
        } else if let Some(after) = after_id {
            // 返回 after_id 之后的消息（更新的消息）
            let start_key = (conv_id as u128) << 32 | (after as u128 + 1);
            let end_key = ((conv_id as u128) << 32) | u64::MAX as u128;
            let range = msgs_table
                .range(start_key..=end_key)
                .map_err(|e| AppError::Database(e.to_string()))?;
            for result in range {
                let (_key, val) = result.map_err(|e| AppError::Database(e.to_string()))?;
                let msg: PrivateMessage = serde_json::from_str(val.value())
                    .map_err(|e| AppError::Internal(e.to_string()))?;
                msgs.push(msg);
                if msgs.len() >= limit as usize {
                    break;
                }
            }
        } else {
            // 默认返回最新的 limit 条
            let start_key = (conv_id as u128) << 32 | 1;
            let end_key = ((conv_id as u128) << 32) | u64::MAX as u128;
            let range = msgs_table
                .range(start_key..=end_key)
                .map_err(|e| AppError::Database(e.to_string()))?;
            for result in range.rev() {
                let (_key, val) = result.map_err(|e| AppError::Database(e.to_string()))?;
                let msg: PrivateMessage = serde_json::from_str(val.value())
                    .map_err(|e| AppError::Internal(e.to_string()))?;
                msgs.push(msg);
                if msgs.len() >= limit as usize {
                    break;
                }
            }
        }
        if before_id.is_some() || (before_id.is_none() && after_id.is_none()) {
            msgs.reverse();
        }
        Ok(msgs)
    }

    pub fn get_group_messages_paginated(
        &self,
        group_id: i64,
        limit: i64,
        before_id: Option<i64>,
        after_id: Option<i64>,
    ) -> Result<Vec<GroupMessage>, AppError> {
        let db = self.db.lock().unwrap();
        let read_txn = db
            .begin_read()
            .map_err(|e| AppError::Database(e.to_string()))?;

        let msgs_table = read_txn
            .open_table(GROUP_MSGS)
            .map_err(|e| AppError::Database(e.to_string()))?;

        let mut msgs = Vec::new();

        if let Some(before) = before_id {
            let end_key = (group_id as u128) << 32 | before as u128;
            let start_key = (group_id as u128) << 32 | 1;
            let range = msgs_table
                .range(start_key..end_key)
                .map_err(|e| AppError::Database(e.to_string()))?;
            for result in range.rev() {
                let (_key, val) = result.map_err(|e| AppError::Database(e.to_string()))?;
                let msg: GroupMessage = serde_json::from_str(val.value())
                    .map_err(|e| AppError::Internal(e.to_string()))?;
                msgs.push(msg);
                if msgs.len() >= limit as usize {
                    break;
                }
            }
        } else if let Some(after) = after_id {
            let start_key = (group_id as u128) << 32 | (after as u128 + 1);
            let end_key = ((group_id as u128) << 32) | u64::MAX as u128;
            let range = msgs_table
                .range(start_key..=end_key)
                .map_err(|e| AppError::Database(e.to_string()))?;
            for result in range {
                let (_key, val) = result.map_err(|e| AppError::Database(e.to_string()))?;
                let msg: GroupMessage = serde_json::from_str(val.value())
                    .map_err(|e| AppError::Internal(e.to_string()))?;
                msgs.push(msg);
                if msgs.len() >= limit as usize {
                    break;
                }
            }
        } else {
            let start_key = (group_id as u128) << 32 | 1;
            let end_key = ((group_id as u128) << 32) | u64::MAX as u128;
            let range = msgs_table
                .range(start_key..=end_key)
                .map_err(|e| AppError::Database(e.to_string()))?;
            for result in range.rev() {
                let (_key, val) = result.map_err(|e| AppError::Database(e.to_string()))?;
                let msg: GroupMessage = serde_json::from_str(val.value())
                    .map_err(|e| AppError::Internal(e.to_string()))?;
                msgs.push(msg);
                if msgs.len() >= limit as usize {
                    break;
                }
            }
        }
        if before_id.is_some() || (before_id.is_none() && after_id.is_none()) {
            msgs.reverse();
        }
        Ok(msgs)
    }
}

fn conversation_key(a: u64, b: u64) -> u64 {
    let min = a.min(b);
    let max = a.max(b);
    (min << 32) | max
}
