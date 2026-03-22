# OpenFang Security System Analysis

*Analysis date: 2026-03-18*

---

## Overview

OpenFang implements **16 independent defense-in-depth layers** so that failure of any single mechanism is caught by others. This document covers the secret management system, capability-based permissions, tool security enforcement, and best practices for custom tool authors.

---

## 1. Secret Management & Credential Vault

**File:** `crates/openfang-extensions/src/vault.rs`

### Architecture
- **AES-256-GCM encrypted storage** with authenticated encryption against tampering
- **Master key management** via OS keyring (Windows Credential Manager, macOS Keychain, Linux Secret Service) with fallback to `OPENFANG_VAULT_KEY` env var
- **Argon2id key derivation** — prevents rainbow table attacks
- **Per-entry zeroization** via `Zeroizing<String>` — all secrets wiped from memory on drop
- **Vault file format:** Magic header `"OFV1"` + versioning for forward compatibility
- **Persistence:** `~/.openfang/vault.enc` (encrypted JSON entries)
- **Machine fingerprint:** SHA-256 of `username+hostname` prevents vault portability attacks — stealing `vault.enc` from one machine won't work on another

### Key Methods
| Method | Purpose |
|--------|---------|
| `vault.init()` | Generates random 256-bit master key, stores in OS keyring |
| `vault.unlock()` | Decrypts vault using resolved master key |
| `vault.get(key)` | Returns `Zeroizing<String>` (auto-wiped on drop) |
| `vault.set(key, value)` | Stores encrypted secret, saves to disk |

### Critical Design Principle
API keys are resolved from the vault at runtime — they are **NOT** stored in env vars or config files. Tools access secrets via vault, not through inherited environment variables.

---

## 2. Capability-Based Permissions System

**File:** `crates/openfang-types/src/capability.rs`

### Core Design
Granular `Capability` enum enforced at kernel level. Agents can **only** do what they have explicit grants for.

### Capability Categories
| Category | Examples |
|----------|---------|
| **File System** | `FileRead("*.md")`, `FileWrite("/data/*")` |
| **Network** | `NetConnect("api.openai.com:443")`, `NetListen(8080)` |
| **Tools** | `ToolInvoke("web_search")`, `ToolAll` (dangerous) |
| **LLM** | `LlmQuery("claude*")`, `LlmMaxTokens(100000)` |
| **Agents** | `AgentSpawn`, `AgentMessage("*")`, `AgentKill("agent-*")` |
| **Memory** | `MemoryRead("shared.*")`, `MemoryWrite("self.*")` |
| **Shell** | `ShellExec("ls")` — no metacharacters allowed |
| **Env Vars** | `EnvRead("PATH")` |
| **Economic** | `EconSpend(50.0)` (USD budget), `EconTransfer("bank-agent")` |

### Pattern Matching
- **Exact:** `"api.openai.com:443"` == `"api.openai.com:443"`
- **Wildcard:** `"*"` matches anything
- **Glob:** `"*.openai.com:443"` matches `"api.openai.com:443"`
- **Numeric bounds:** `LlmMaxTokens(10000)` grants `LlmMaxTokens(5000)` ✓

### Privilege Escalation Prevention
```rust
fn validate_capability_inheritance(parent_caps, child_caps) {
    for child_cap in child_caps {
        let is_covered = parent_caps.iter()
            .any(|parent_cap| capability_matches(parent_cap, child_cap));
        if !is_covered {
            return Err("Privilege escalation denied");
        }
    }
}
```
**Child agents cannot exceed parent permissions** — enforced at spawn time.

### Agent Manifest Declaration (`agents/*/agent.toml`)
```toml
[capabilities]
tools = ["file_read", "file_write", "memory_store", "memory_recall", "web_fetch"]
network = ["*"]
memory_read = ["*"]
memory_write = ["self.*", "shared.*"]
shell = ["cargo clippy *", "git diff *"]   # only specific commands
```

---

## 3. Tool Execution Security Pipeline

**File:** `crates/openfang-runtime/src/tool_runner.rs`

Every tool invocation passes through this sequence:

### Step 1: Capability Check
```rust
if let Some(allowed) = allowed_tools {
    if !allowed.iter().any(|t| t == tool_name) {
        return ToolResult { is_error: true, content: "Permission denied" };
    }
}
```
Prevents LLM hallucination of unavailable tools.

### Step 2: Approval Gate
High-risk tools check `kernel.requires_approval(tool_name)` — blocks until a human approves via dashboard. Used by payment and purchase tools.

### Step 3: Taint-Aware Dispatch

**Web Fetch — credential exfiltration check:**
```rust
// Blocks URLs with api_key=, token=, secret=, password= in query string
if let Some(violation) = check_taint_net_fetch(url) {
    return ToolResult { is_error: true, content: format!("Taint violation: {violation}") };
}
```

**Shell — injection check:**
```rust
// Applied BEFORE exec policy (even "Full" mode blocks these)
if let Some(reason) = contains_shell_metacharacters(command) {
    return ToolResult { is_error: true, content: format!("Shell blocked: {reason}") };
}
```

### Inter-Agent Depth Limit
```rust
const MAX_AGENT_CALL_DEPTH: u32 = 5;
```
Prevents infinite agent recursion (A→B→C→...→Z).

---

## 4. Information Flow Taint Tracking

**File:** `crates/openfang-types/src/taint.rs`

### Taint Labels
| Label | Meaning |
|-------|---------|
| `ExternalNetwork` | Data from HTTP responses / web_fetch |
| `UserInput` | Directly from user prompts |
| `Pii` | Personally identifiable information |
| `Secret` | API keys, tokens, passwords |
| `UntrustedAgent` | Output from sandboxed/low-privilege agents |

### Sink Enforcement
| Sink | Blocked Labels | Why |
|------|----------------|-----|
| `shell_exec()` | ExternalNetwork, UntrustedAgent, UserInput | Prevent injection |
| `net_fetch()` | Secret, Pii | Prevent exfiltration |
| `agent_message()` | Secret | Prevent leaking credentials to other agents |

### Declassification
```rust
pub fn declassify(&mut self, label: &TaintLabel) {
    self.labels.remove(label);
}
```
Explicit caller assertion that the value has been sanitized — must be intentional, never automatic.

---

## 5. SSRF Protection

**File:** `crates/openfang-runtime/src/web_fetch.rs`

Four independent defense layers — `check_ssrf(url)` called before any network I/O:

**Layer 1 — Hostname blocklist:**
```
localhost, ip6-localhost,
metadata.google.internal, metadata.aws.internal,
169.254.169.254 (AWS IMDS),
100.100.100.200 (Alibaba Cloud IMDS),
192.0.0.192 (Azure IMDS),
0.0.0.0, ::1, [::1]
```

**Layer 2 — DNS resolution + IP classification:**
Resolves hostname, then rejects loopback, unspecified, and private IPs:
- IPv4 private: `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `169.254.0.0/16`
- IPv6 ULA: `fc00::/7`, link-local: `fe80::/10`

**Layer 3 — Scheme allowlist:**
Only `http://` and `https://` — blocks `file://`, `ftp://`, `gopher://`, etc.

**Layer 4 — IPv6 bracket notation:**
Correctly parses `[::1]:8080` notation to prevent bypass via IPv6 tricks.

---

## 6. Subprocess Sandboxing

**File:** `crates/openfang-runtime/src/subprocess_sandbox.rs`

### Environment Isolation
```rust
pub fn sandbox_command(cmd: &mut tokio::process::Command, allowed_env_vars: &[String]) {
    cmd.env_clear();  // inherit NOTHING by default

    // Re-add safe vars only
    for var in SAFE_ENV_VARS { cmd.env(var, ..); }
    for var in allowed_env_vars { cmd.env(var, ..); }
}
```
**Safe list:** `PATH, HOME, TMPDIR, TEMP, LANG, LC_ALL, TERM`
API keys and secrets in the parent env are **never inherited** by subprocesses.

### Shell Metacharacter Blocking
Blocked characters: `` ` ``, `$(`, `${`, `;`, `|`, `>`, `<`, `{`, `}`, `&`, `\n`, `\0`

This is applied **before** exec policy checks — even unrestricted shell mode rejects metacharacters.

### Exec Policy Modes
| Mode | Description |
|------|-------------|
| `Deny` | No shell execution |
| `Allowlist` | Only commands in `policy.allowed_commands` + metacharacter check |
| `Full` | Any command, but still rejects metacharacters and taint |

### Path Traversal Prevention
Rejects any executable path containing `..` components.

---

## 7. WASM Sandbox (Dual Metering)

**File:** `crates/openfang-runtime/src/sandbox.rs`

Custom tools can be distributed as WASM modules with strong isolation:

```rust
pub struct SandboxConfig {
    pub fuel_limit: u64,               // CPU instruction budget
    pub max_memory_bytes: usize,       // 16MB default
    pub capabilities: Vec<Capability>, // Granted permissions
    pub timeout_secs: Option<u64>,     // 30s default
}
```

**Dual metering:**
1. **Fuel (CPU budget)** — wasmtime instruction counter, configurable per skill
2. **Epoch timeout** — wall-clock watchdog thread interrupts runaway execution

**Guest ABI:**
```
export memory
export alloc(size: i32) -> i32
export execute(input_ptr: i32, input_len: i32) -> i64
```

**Security boundary:**
- Guest code has **zero filesystem/network access** by default
- Host functions check capabilities before servicing any request
- Output subject to same taint and validation rules as built-in tools

---

## 8. Authentication & Session Tokens

**Files:** `crates/openfang-kernel/src/auth.rs`, `crates/openfang-api/src/session_auth.rs`

### RBAC Role Hierarchy
| Role | Access |
|------|--------|
| `Viewer` | Read-only |
| `User` | Chat with agents |
| `Admin` | Spawn/kill agents, install skills, view usage |
| `Owner` | Full access + user management |

### Stateless Session Tokens
Format: `base64(username:expiry:hmac-sha256(username:expiry))`
- No server-side state required
- TTL-enforced via expiry timestamp
- **Constant-time comparison** using the `subtle` crate to prevent timing attacks

### Channel Binding
Maps external platform identities (Telegram ID, Discord user, etc.) to internal OpenFang users at kernel level.

---

## 9. Rate Limiting (GCRA)

**File:** `crates/openfang-api/src/rate_limiter.rs`

Cost-aware token bucket, 500 tokens/minute/IP:

| Operation | Token Cost |
|-----------|-----------|
| Health check | 1 |
| List endpoints (GET) | 2–5 |
| Send message | 30 |
| Spawn agent / install skill | 50 |
| Run workflow | 100 |

Returns `429 Too Many Requests` with `Retry-After: 60` header when budget exhausted.

---

## 10. Merkle Hash Chain Audit Trail

**File:** `crates/openfang-runtime/src/audit.rs`

Every security-relevant action is recorded in a tamper-evident chain:

```rust
pub struct AuditEntry {
    pub seq: u64,            // Monotonically increasing
    pub agent_id: String,    // Actor
    pub action: AuditAction, // ToolInvoke, AgentSpawn, FileAccess, etc.
    pub detail: String,      // "web_fetch: https://api.example.com"
    pub outcome: String,     // "ok", "denied", error message
    pub prev_hash: String,   // SHA-256 of previous entry
    pub hash: String,        // SHA-256(all fields + prev_hash)
}
```

Modifying any historical entry invalidates all downstream hashes. Chain is persisted to SQLite (migration v8) and verified on daemon restart.

**Audited event types:** ToolInvoke, CapabilityCheck, AgentSpawn, AgentKill, AgentMessage, MemoryAccess, FileAccess, NetworkAccess, ShellExec, AuthAttempt, WireConnect, ConfigChange.

---

## 11. Manifest Signing (Ed25519)

- Agent manifests are **Ed25519 signed** — verified at spawn time
- Prevents skill/agent tampering and ensures authenticity
- Wire protocol uses **HMAC-SHA256** with nonce-based replay protection for P2P mutual authentication

---

## 12. Security-Critical Dependencies

| Crate | Purpose |
|-------|---------|
| `aes-gcm` | AES-256-GCM vault encryption |
| `argon2` | Argon2id key derivation for vault master key |
| `ed25519-dalek` | Manifest signing (side-channel resistant) |
| `sha2` | Hash chain, checksums |
| `hmac` | Wire protocol authentication |
| `subtle` | Constant-time comparisons (timing attack prevention) |
| `zeroize` | Volatile memory wipe for secrets |
| `rand` | OsRng for cryptographic randomness |
| `governor` | GCRA rate limiting |
| `wasmtime` | WASM sandbox with fuel/epoch metering |

---

## 13. Best Practices for Custom Tool Authors

### Secret Access — Use the Vault, Not Env Vars

**Wrong:**
```rust
let api_key = std::env::var("MY_API_KEY").unwrap();
```

**Correct:**
```rust
let api_key = vault.get("my_api_key")?;
// Zeroizing<String> — wiped from memory when it goes out of scope
```

### Declare Minimal Capabilities in `agent.toml`
```toml
[capabilities]
tools = ["web_fetch", "memory_store"]  # only what you need
network = ["api.specific-service.com:443"]  # not "*"
memory_write = ["self.*"]  # not "shared.*" unless required
# NO shell access unless absolutely necessary
```
Never use `ToolAll` — it bypasses the entire permission model.

### Never Log Secrets
```rust
// WRONG
log::info!("Calling API with key: {}", api_key);

// CORRECT
log::info!("Calling API with configured key");
```
The audit trail records actions and outcomes — never include credential values.

### Validate Inputs at Trust Boundaries
- Validate type, length, format, and range for all external inputs
- Use schema validation before passing data to tools
- Apply `TaintLabel::ExternalNetwork` to data from web responses before passing to shell or other agents

### Explicitly Declassify Tainted Data
```rust
// Data from web_fetch carries ExternalNetwork taint
let response = web_fetch(url).await?;
let mut tainted = TaintedValue::new(response, labels, "web_fetch");

// Only after validation:
let validated = sanitize_and_extract(&tainted.value)?;
let mut clean = TaintedValue::new(validated, HashSet::new(), "sanitized");
// Now safe to pass to shell or agent
```

### Approval Gates for Sensitive Operations
Register high-risk tool operations with the kernel approval system so humans stay in the loop:
```toml
# In HAND.toml or agent.toml
[[settings]]
key = "approval_mode"
label = "Require Approval"
description = "Require user confirmation before performing this action"
default = "true"
```

### WASM Modules — Request Only Needed Capabilities
```rust
pub struct MyToolManifest {
    capabilities: vec![
        Capability::NetConnect("api.target.com:443"),
        // NOT Capability::ToolAll
    ],
    fuel_limit: 1_000_000,   // Set a reasonable CPU budget
    timeout_secs: Some(10),  // Don't hang indefinitely
}
```

### Handle Secrets — Never Pass to LLM Context
Secrets resolved from vault should be used in HTTP headers or tool parameters directly — never interpolated into prompts, system instructions, or memory fragments that could appear in LLM context.

---

## Security Layer Summary

| Layer | Mechanism | File |
|-------|-----------|------|
| Secret Storage | AES-256-GCM + OS keyring + Argon2id KDF | `vault.rs` |
| Access Control | Capability grants + privilege escalation prevention | `capability.rs` |
| Tool Enforcement | Capability check → approval gate → taint validation | `tool_runner.rs` |
| Data Flow | Taint tracking blocks credentials/PII at sinks | `taint.rs` |
| Network Safety | SSRF: hostname blocklist + DNS check + scheme allowlist | `web_fetch.rs` |
| Subprocess Safety | `env_clear()` + metacharacter blocking + exec allowlist | `subprocess_sandbox.rs` |
| WASM Isolation | Fuel + epoch dual metering + capability-gated host_call | `sandbox.rs` |
| User Auth | RBAC + HMAC-signed stateless tokens + constant-time compare | `auth.rs`, `session_auth.rs` |
| Rate Limiting | Cost-aware GCRA (500 tokens/min/IP) | `rate_limiter.rs` |
| Audit Trail | Merkle hash chain (tamper-evident, SQLite-persisted) | `audit.rs` |
| Manifest Signing | Ed25519 digital signatures | `verify.rs` |
| Approval Workflows | Human-in-the-loop for high-risk operations | `tool_runner.rs:146` |
