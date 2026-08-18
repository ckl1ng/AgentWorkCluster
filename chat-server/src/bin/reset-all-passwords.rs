use std::env;
use std::path::Path;

use redb::{Database, ReadableTable, TableDefinition};
use serde::Deserialize;

const USERS: TableDefinition<u64, &str> = TableDefinition::new("users");
const USER_PASSWORDS: TableDefinition<u64, &str> = TableDefinition::new("user_passwords");
const PASSWORD_LENGTH: usize = 24;
const PASSWORD_CHARS: &[u8] = b"ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789";

#[derive(Deserialize)]
struct UserRecord {
    id: i64,
    username: String,
}

fn generate_password() -> Result<String, String> {
    let mut random = [0u8; PASSWORD_LENGTH];
    getrandom::getrandom(&mut random).map_err(|err| format!("无法生成随机密码: {err}"))?;
    Ok(random
        .iter()
        .map(|byte| PASSWORD_CHARS[*byte as usize % PASSWORD_CHARS.len()] as char)
        .collect())
}

fn reset_all_passwords(path: &Path) -> Result<Vec<(String, String)>, String> {
    let db = Database::open(path).map_err(|err| format!("无法打开数据库: {err}"))?;

    let users = {
        let read_txn = db
            .begin_read()
            .map_err(|err| format!("无法读取数据库: {err}"))?;
        let users_table = read_txn
            .open_table(USERS)
            .map_err(|err| format!("无法读取用户表: {err}"))?;
        let mut users = Vec::new();
        for entry in users_table
            .iter()
            .map_err(|err| format!("无法遍历用户表: {err}"))?
        {
            let (_, value) = entry.map_err(|err| format!("无法读取用户记录: {err}"))?;
            let user: UserRecord = serde_json::from_str(value.value())
                .map_err(|err| format!("用户记录格式错误: {err}"))?;
            users.push(user);
        }
        users
    };

    if users.is_empty() {
        return Err("数据库中没有可重置密码的账号".to_string());
    }

    let credentials: Vec<(i64, String, String, String)> = users
        .into_iter()
        .map(|user| {
            let password = generate_password()?;
            let hash = bcrypt::hash(&password, bcrypt::DEFAULT_COST)
                .map_err(|err| format!("无法生成密码哈希: {err}"))?;
            Ok((user.id, user.username, password, hash))
        })
        .collect::<Result<_, String>>()?;

    let write_txn = db
        .begin_write()
        .map_err(|err| format!("无法写入数据库: {err}"))?;
    {
        let mut passwords = write_txn
            .open_table(USER_PASSWORDS)
            .map_err(|err| format!("无法打开密码表: {err}"))?;
        for (user_id, _, _, hash) in &credentials {
            passwords
                .insert(*user_id as u64, hash.as_str())
                .map_err(|err| format!("无法写入密码哈希: {err}"))?;
        }
    }
    write_txn
        .commit()
        .map_err(|err| format!("无法提交密码重置: {err}"))?;

    Ok(credentials
        .into_iter()
        .map(|(_, username, password, _)| (username, password))
        .collect())
}

fn main() {
    let mut args = env::args_os();
    let program = args.next().unwrap_or_default();
    let Some(database_path) = args.next() else {
        eprintln!("用法: {} <chat.db 路径>", program.to_string_lossy());
        std::process::exit(2);
    };
    if args.next().is_some() {
        eprintln!("只接受一个 chat.db 路径参数");
        std::process::exit(2);
    }

    match reset_all_passwords(Path::new(&database_path)) {
        Ok(credentials) => {
            println!("# 已重置 {} 个账号的密码", credentials.len());
            println!("# 用户名\t新密码");
            for (username, password) in credentials {
                println!("{username}\t{password}");
            }
        }
        Err(err) => {
            eprintln!("重置失败: {err}");
            std::process::exit(1);
        }
    }
}
