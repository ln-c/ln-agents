//! A fake `ln-pipeline` A2A server for tests: one thread on 127.0.0.1:<free port> that answers
//! each HTTP request with whatever the test's handler returns, and records what it was sent.
//! Shared by ln-a2a's and ln-agents's tests (`#[path]`-included).

#![allow(dead_code)]

use std::collections::HashMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use serde_json::Value;

pub const TOKEN: &str = "fake-token-0123456789abcdefghijklmnopqrstuvwxyz";

#[derive(Debug, Clone)]
pub struct Seen {
    pub method: String,
    pub path: String,
    pub headers: HashMap<String, String>,
    pub body: Value,
}

impl Seen {
    /// The JSON-RPC method, or "" for a GET.
    pub fn rpc(&self) -> &str {
        self.body
            .get("method")
            .and_then(Value::as_str)
            .unwrap_or_default()
    }

    /// The single data part of the A2A message sent, if any.
    pub fn data(&self) -> Option<&Value> {
        self.body.pointer("/params/message/parts/0/data")
    }
}

pub struct Reply {
    pub status: u16,
    pub content_type: &'static str,
    pub body: String,
    /// Keep the connection open this long after the body (an SSE stream still running).
    pub hold: Option<Duration>,
    /// Wait this long before answering at all (a server still starting the run).
    pub delay: Option<Duration>,
}

impl Reply {
    pub fn json(body: impl Into<String>) -> Self {
        Reply {
            status: 200,
            content_type: "application/json",
            body: body.into(),
            hold: None,
            delay: None,
        }
    }

    pub fn sse(body: impl Into<String>) -> Self {
        Reply {
            status: 200,
            content_type: "text/event-stream",
            body: body.into(),
            hold: None,
            delay: None,
        }
    }

    /// An SSE stream that sends `body` and then stays open for `hold`.
    pub fn stalled_sse(body: impl Into<String>, hold: Duration) -> Self {
        Reply {
            hold: Some(hold),
            ..Reply::sse(body)
        }
    }
}

pub struct Fake {
    pub url: String,
    pub seen: Arc<Mutex<Vec<Seen>>>,
}

impl Fake {
    pub fn start(handler: impl Fn(&Seen) -> Reply + Send + Sync + 'static) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind");
        let port = listener.local_addr().expect("addr").port();
        let seen = Arc::new(Mutex::new(Vec::new()));
        let log = Arc::clone(&seen);
        let handler = Arc::new(handler);
        thread::spawn(move || {
            for stream in listener.incoming().flatten() {
                let log = Arc::clone(&log);
                let handler = Arc::clone(&handler);
                thread::spawn(move || serve(stream, &log, handler.as_ref()));
            }
        });
        Fake {
            url: format!("http://127.0.0.1:{port}/a2a/jsonrpc"),
            seen,
        }
    }

    pub fn seen(&self) -> Vec<Seen> {
        self.seen.lock().expect("lock").clone()
    }

    /// The JSON-RPC methods received so far, in order.
    pub fn methods(&self) -> Vec<String> {
        self.seen().iter().map(|s| s.rpc().to_string()).collect()
    }
}

fn serve(
    stream: TcpStream,
    log: &Mutex<Vec<Seen>>,
    handler: &(dyn Fn(&Seen) -> Reply + Send + Sync),
) {
    let mut reader = BufReader::new(stream.try_clone().expect("clone"));
    let mut line = String::new();
    if reader.read_line(&mut line).unwrap_or(0) == 0 {
        return;
    }
    let mut parts = line.split_whitespace();
    let method = parts.next().unwrap_or_default().to_string();
    let path = parts.next().unwrap_or_default().to_string();
    let mut headers = HashMap::new();
    loop {
        let mut header = String::new();
        if reader.read_line(&mut header).unwrap_or(0) == 0 || header.trim().is_empty() {
            break;
        }
        if let Some((k, v)) = header.split_once(':') {
            headers.insert(k.trim().to_ascii_lowercase(), v.trim().to_string());
        }
    }
    let length: usize = headers
        .get("content-length")
        .and_then(|v| v.parse().ok())
        .unwrap_or(0);
    let mut body = vec![0; length];
    reader.read_exact(&mut body).expect("body");
    let seen = Seen {
        method,
        path,
        headers,
        body: serde_json::from_slice(&body).unwrap_or(Value::Null),
    };
    log.lock().expect("lock").push(seen.clone());
    let reply = handler(&seen);
    if let Some(delay) = reply.delay {
        thread::sleep(delay);
    }
    let mut stream = stream;
    // A held stream has no length: its body ends when the connection closes.
    let length = match reply.hold {
        Some(_) => String::new(),
        None => format!("Content-Length: {}\r\n", reply.body.len()),
    };
    let head = format!(
        "HTTP/1.1 {} X\r\nContent-Type: {}\r\n{length}Connection: close\r\n\r\n",
        reply.status, reply.content_type,
    );
    let _ = stream.write_all(head.as_bytes());
    let _ = stream.write_all(reply.body.as_bytes());
    let _ = stream.flush();
    if let Some(hold) = reply.hold {
        thread::sleep(hold);
    }
}

/// The JSON fixtures captured from the real server (ln-a2a/tests/fixtures).
pub fn fixture(name: &str) -> String {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR"));
    let path = [
        dir.join("tests/fixtures"),
        dir.join("../ln-a2a/tests/fixtures"),
    ]
    .into_iter()
    .map(|d| d.join(name))
    .find(|p| p.exists())
    .unwrap_or_else(|| PathBuf::from(name));
    std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("{}: {e}", path.display()))
}

/// A 0600 token file holding [`TOKEN`] under `dir`.
pub fn token_file(dir: &Path) -> PathBuf {
    let path = dir.join("token");
    std::fs::write(&path, format!("{TOKEN}\n")).expect("write token");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).expect("chmod");
    }
    path
}

/// A fresh empty directory for one test.
pub fn scratch(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("ln-agents-test-{}-{name}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).expect("scratch dir");
    dir
}
