use std::collections::HashMap;
use std::sync::Arc;

use tokio::sync::{broadcast, RwLock};
use tracing::info;

#[derive(Debug, Clone)]
pub struct DeliveryMessage {
    pub msg_type: String,
    pub message_id: i64,
    pub from_user_id: i64,
    pub from_username: String,
    pub from_avatar: String,
    pub group_id: Option<i64>,
    pub encrypted_content: Vec<u8>,
    pub content_type: String,
    pub created_at: String,
    pub client_message_id: Option<String>,
}

#[derive(Debug, Clone)]
pub struct PresenceEvent {
    pub user_id: i64,
    pub online: bool,
}

pub struct ConnectionManager {
    // A count, rather than a boolean, keeps a user online while another tab remains connected.
    online: RwLock<HashMap<i64, usize>>,
    channels: RwLock<HashMap<i64, broadcast::Sender<DeliveryMessage>>>,
    presence: broadcast::Sender<PresenceEvent>,
}

impl ConnectionManager {
    pub fn new() -> Arc<Self> {
        let (presence, _) = broadcast::channel(256);
        Arc::new(Self {
            online: RwLock::new(HashMap::new()),
            channels: RwLock::new(HashMap::new()),
            presence,
        })
    }

    pub async fn user_online(
        &self,
        user_id: i64,
    ) -> (
        broadcast::Receiver<DeliveryMessage>,
        broadcast::Receiver<PresenceEvent>,
        bool,
    ) {
        let became_online = {
            let mut online = self.online.write().await;
            let connections = online.entry(user_id).or_insert(0);
            let became_online = *connections == 0;
            *connections += 1;
            became_online
        };
        let receiver = {
            let mut channels = self.channels.write().await;
            match channels.get(&user_id) {
                Some(tx) => tx.subscribe(),
                None => {
                    let (tx, rx) = broadcast::channel(256);
                    channels.insert(user_id, tx);
                    rx
                }
            }
        };
        info!("用户 {} 上线", user_id);
        (receiver, self.presence.subscribe(), became_online)
    }

    pub async fn user_offline(&self, user_id: i64) -> bool {
        let mut online = self.online.write().await;
        let Some(connections) = online.get_mut(&user_id) else {
            return false;
        };
        *connections -= 1;
        if *connections == 0 {
            online.remove(&user_id);
            info!("用户 {} 下线", user_id);
            true
        } else {
            false
        }
    }

    pub async fn online_users(&self) -> Vec<i64> {
        self.online.read().await.keys().copied().collect()
    }

    pub fn publish_presence(&self, user_id: i64, online: bool) {
        let _ = self.presence.send(PresenceEvent { user_id, online });
    }

    pub async fn send_to_user(&self, user_id: i64, msg: DeliveryMessage) -> bool {
        if !self.online.read().await.contains_key(&user_id) {
            return false;
        }
        let channels = self.channels.read().await;
        channels
            .get(&user_id)
            .map(|tx| tx.send(msg).is_ok())
            .unwrap_or(false)
    }

    pub async fn send_to_group(&self, msg: DeliveryMessage, members: &[i64]) {
        let online = self.online.read().await;
        let channels = self.channels.read().await;
        for &user_id in members {
            if online.contains_key(&user_id) {
                if let Some(tx) = channels.get(&user_id) {
                    let _ = tx.send(msg.clone());
                }
            }
        }
    }
}
