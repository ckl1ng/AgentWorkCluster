// 服务端加密工具
// 注意：服务端永远不接触明文消息内容！
// 此模块仅用于：
// 1. 公钥格式验证（长度检查）
// 2. 生成随机 token（用于连接认证）

use rand::Rng;

/// 初始化，无需操作
pub fn init() {}

/// 验证公钥是否是合法的 32 字节（Curve25519 公钥长度）
pub fn validate_public_key(key_bytes: &[u8]) -> bool {
    key_bytes.len() == 32
}

/// 生成一个随机 token（32 字节 hex）
pub fn generate_token() -> String {
    let mut buf = [0u8; 32];
    rand::thread_rng().fill(&mut buf);
    hex_encode(&buf)
}

fn hex_encode(bytes: &[u8]) -> String {
    const CHARS: &[u8] = b"0123456789abcdef";
    let mut out = Vec::with_capacity(bytes.len() * 2);
    for &b in bytes {
        out.push(CHARS[(b >> 4) as usize]);
        out.push(CHARS[(b & 0x0f) as usize]);
    }
    unsafe { String::from_utf8_unchecked(out) }
}
