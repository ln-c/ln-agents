//! Newline-delimited JSON-RPC 2.0 over stdio, as ACP frames it: one message per line.
//!
//! Only JSON-RPC lines go to the output, each flushed as it is written; anything else (logs)
//! goes to stderr. Requests this side sends wait on a channel the reader fills when the
//! matching response arrives.

use std::collections::HashMap;
use std::io::Write;
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::mpsc::{Receiver, Sender, channel};
use std::sync::{Arc, Mutex};

use serde::Serialize;
use serde_json::{Value, json};

/// JSON-RPC error codes this agent answers with.
pub const PARSE_ERROR: i64 = -32700;
pub const INVALID_REQUEST: i64 = -32600;
pub const METHOD_NOT_FOUND: i64 = -32601;
pub const INVALID_PARAMS: i64 = -32602;

/// One parsed incoming line.
#[derive(Debug)]
pub enum Incoming {
    Request {
        id: Value,
        method: String,
        params: Value,
    },
    Notification {
        method: String,
        params: Value,
    },
    Response {
        id: Value,
        outcome: Result<Value, Value>,
    },
}

/// Classify a line; `Err` carries the JSON-RPC error to answer with (id null).
pub fn parse(line: &str) -> Result<Incoming, (i64, String)> {
    let message: Value =
        serde_json::from_str(line).map_err(|e| (PARSE_ERROR, format!("not JSON: {e}")))?;
    let Value::Object(mut fields) = message else {
        return Err((INVALID_REQUEST, "a JSON-RPC message is an object".into()));
    };
    let params = fields.remove("params").unwrap_or(Value::Null);
    match (fields.remove("id"), fields.remove("method")) {
        (Some(id), Some(Value::String(method))) => Ok(Incoming::Request { id, method, params }),
        (None, Some(Value::String(method))) => Ok(Incoming::Notification { method, params }),
        (Some(id), None) => {
            let outcome = match fields.remove("error") {
                Some(error) => Err(error),
                None => Ok(fields.remove("result").unwrap_or(Value::Null)),
            };
            Ok(Incoming::Response { id, outcome })
        }
        _ => Err((
            INVALID_REQUEST,
            "neither a request, a notification nor a response".into(),
        )),
    }
}

/// What a request this side sent comes back as: its result, or its error object.
pub type Outcome = Result<Value, Value>;

/// The write side of the connection, shared by every thread that answers or notifies.
#[derive(Clone)]
pub struct Connection {
    output: Arc<Mutex<Box<dyn Write + Send>>>,
    pending: Arc<Mutex<HashMap<i64, Sender<Outcome>>>>,
    next_id: Arc<AtomicI64>,
}

impl Connection {
    pub fn new(output: impl Write + Send + 'static) -> Self {
        Connection {
            output: Arc::new(Mutex::new(Box::new(output))),
            pending: Arc::default(),
            next_id: Arc::new(AtomicI64::new(0)),
        }
    }

    fn write(&self, message: &Value) {
        let mut line = serde_json::to_string(message).expect("JSON values always serialize");
        line.push('\n');
        let mut output = self.output.lock().unwrap_or_else(|e| e.into_inner());
        // A closed stdout means the editor is gone; the reader sees EOF and stops.
        let _ = output
            .write_all(line.as_bytes())
            .and_then(|()| output.flush());
    }

    pub fn respond(&self, id: &Value, result: impl Serialize) {
        let result = serde_json::to_value(result).expect("ACP types serialize");
        self.write(&json!({"jsonrpc": "2.0", "id": id, "result": result}));
    }

    pub fn respond_error(&self, id: &Value, code: i64, message: &str) {
        self.write(
            &json!({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}),
        );
    }

    pub fn notify(&self, method: &str, params: impl Serialize) {
        let params = serde_json::to_value(params).expect("ACP types serialize");
        self.write(&json!({"jsonrpc": "2.0", "method": method, "params": params}));
    }

    /// Send a request to the client; the receiver yields its result or error.
    pub fn request(&self, method: &str, params: impl Serialize) -> Receiver<Outcome> {
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        let (tx, rx) = channel();
        self.pending
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert(id, tx);
        let params = serde_json::to_value(params).expect("ACP types serialize");
        self.write(&json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params}));
        rx
    }

    /// Hand a response to the request waiting for it; unknown ids are dropped.
    pub fn resolve(&self, id: &Value, outcome: Outcome) {
        let Some(id) = id.as_i64() else { return };
        let waiting = self
            .pending
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .remove(&id);
        if let Some(tx) = waiting {
            let _ = tx.send(outcome);
        }
    }

    /// Fail every request still waiting (the input closed).
    pub fn close(&self) {
        let waiting: Vec<_> = self
            .pending
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .drain()
            .collect();
        for (_, tx) in waiting {
            let _ = tx.send(Err(
                json!({"code": INVALID_REQUEST, "message": "connection closed"}),
            ));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[derive(Clone, Default)]
    struct Sink(Arc<Mutex<Vec<u8>>>);

    impl Write for Sink {
        fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
            self.0.lock().unwrap().extend_from_slice(buf);
            Ok(buf.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }

    impl Sink {
        fn lines(&self) -> Vec<Value> {
            let text = String::from_utf8(self.0.lock().unwrap().clone()).unwrap();
            text.lines()
                .map(|l| serde_json::from_str(l).unwrap())
                .collect()
        }
    }

    #[test]
    fn lines_are_classified() {
        let request = r#"{"jsonrpc":"2.0","id":3,"method":"session/new","params":{"cwd":"/"}}"#;
        assert!(
            matches!(parse(request), Ok(Incoming::Request { method, .. }) if method == "session/new")
        );
        let note = r#"{"jsonrpc":"2.0","method":"session/cancel","params":{"sessionId":"s"}}"#;
        assert!(matches!(parse(note), Ok(Incoming::Notification { .. })));
        let reply = r#"{"jsonrpc":"2.0","id":0,"error":{"code":1,"message":"no"}}"#;
        assert!(matches!(
            parse(reply),
            Ok(Incoming::Response {
                outcome: Err(_),
                ..
            })
        ));
        assert_eq!(parse("{nope").unwrap_err().0, PARSE_ERROR);
        assert_eq!(parse("[1]").unwrap_err().0, INVALID_REQUEST);
    }

    #[test]
    fn every_message_is_one_line_and_responses_reach_their_request() {
        let sink = Sink::default();
        let connection = Connection::new(sink.clone());
        connection.respond(&json!(7), json!({"a": "multi\nline"}));
        let answer = connection.request("session/request_permission", json!({}));
        connection.notify("session/update", json!({}));
        let lines = sink.lines();
        assert_eq!(lines.len(), 3);
        assert_eq!(lines[0]["id"], 7);
        let sent_id = lines[1]["id"].clone();
        connection.resolve(&sent_id, Ok(json!({"outcome": "x"})));
        assert_eq!(answer.recv().unwrap().unwrap()["outcome"], "x");
        let orphan = connection.request("x", json!({}));
        connection.close();
        assert!(orphan.recv().unwrap().is_err());
    }
}
