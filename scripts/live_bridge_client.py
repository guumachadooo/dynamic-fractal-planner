#!/usr/bin/env python3
"""Universal Live Device/App Bridge Client v3 (Dynamic Fractal Planner v2.6.0).

Supports two secure session modes over a zero-cost HTTPS relay (e.g. Cloudflare Worker v3):
  1. Direct Ephemeral Code (`--code <session-code>`): connect to an already-open session.
  2. Biometric Background Wake (`--wake --pair-secret <hex>` or env var):
     - Derives public channel ID `X-Device-Hash = sha256("wake-channel-v2:" + pair_secret_hex)`.
     - Signs wake request with HMAC-SHA256 + Unix timestamp + CSPRNG nonce.
     - Device verifies signature in background standby, triggers native BiometricPrompt
       (fingerprint/face/PIN), opens an ephemeral session, and returns the session token
       encrypted end-to-end with Encrypt-then-MAC (`SHA256-CTR + HMAC-SHA256`) so the
       relay never sees the plaintext session code.
  3. Flexible tiered & unrestricted dynamic commands (v3):
     - Predefined Tier 0/1/2 commands (`--cmd <name> --args '<json>'`, `--capabilities`, `--batch`, `--save-screenshot`).
     - Unrestricted Tier 2 dynamic execution: `reflect_types`, `reflect_members`, `reflect_get`,
       `reflect_set`, `reflect_invoke`, `ui_interact`, `shell_exec`, and multi-step `--pipeline` (`$prev` chaining).

Zero third-party dependencies (Python stdlib only). Never writes secrets to disk.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

USER_AGENT = "DynamicFractalPlanner-LiveBridge/3.0"


def validate_https_url(raw_url: str) -> str:
    clean = (raw_url or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(clean)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("Relay URL must use https:// with a valid public host.")
    host = (parsed.hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local"):
        raise ValueError("Relay URL must be a public HTTPS endpoint.")
    return clean


def normalize_pair_secret_hex(raw_secret: str) -> str:
    """Normalize a 256-bit (64-hex) pairing secret (accepts optional CAVPAIR-/PAIR- prefix & hyphens)."""
    s = (raw_secret or "").strip()
    s = re.sub(r"^(?:CAVPAIR|PAIR)[-_:]?", "", s, flags=re.IGNORECASE)
    s = re.sub(r"[\s\-_]", "", s).lower()
    if len(s) != 64 or not re.fullmatch(r"[0-9a-f]{64}", s):
        raise ValueError("Pairing secret must be 64 hexadecimal characters (256-bit key).")
    return s


def compute_wake_channel_hash(pair_secret_hex: str) -> str:
    norm = normalize_pair_secret_hex(pair_secret_hex)
    return hashlib.sha256(f"wake-channel-v2:{norm}".encode("utf-8")).hexdigest()


def sign_wake_request(
    pair_secret_hex: str,
    wake_id: str,
    timestamp_unix: int,
    tier: int,
    duration_seconds: int,
    reason: str,
) -> str:
    key = bytes.fromhex(normalize_pair_secret_hex(pair_secret_hex))
    canonical = f"wake-v2|{wake_id}|{timestamp_unix}|{tier}|{duration_seconds}|{reason}"
    return hmac.new(key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def _ctr_keystream(enc_key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hashlib.sha256(enc_key + nonce + struct.pack(">I", counter)).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def encrypt_approval_payload(pair_secret_hex: str, wake_id: str, plaintext_obj: dict[str, Any], nonce: bytes | None = None) -> dict[str, Any]:
    """Encrypt-then-MAC (SHA256-CTR + HMAC-SHA256) matching Android C# LiveDiagnosticBridgeService."""
    key = bytes.fromhex(normalize_pair_secret_hex(pair_secret_hex))
    iv = nonce if nonce is not None else secrets.token_bytes(16)
    if len(iv) != 16:
        raise ValueError("Nonce must be exactly 16 bytes.")
    enc_key = hmac.new(key, b"enc-v2:" + iv, hashlib.sha256).digest()
    mac_key = hmac.new(key, b"mac-v2:" + iv, hashlib.sha256).digest()
    plain = json.dumps(plaintext_obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ks = _ctr_keystream(enc_key, iv, len(plain))
    ct = bytes(a ^ b for a, b in zip(plain, ks))
    mac_input = b"wake-approve-v2|" + wake_id.encode("utf-8") + b"|" + iv + ct
    tag = hmac.new(mac_key, mac_input, hashlib.sha256).digest()
    return {
        "v": 2,
        "nonce": base64.b64encode(iv).decode("ascii"),
        "ct": base64.b64encode(ct).decode("ascii"),
        "tag": base64.b64encode(tag).decode("ascii"),
    }


def decrypt_approval_payload(pair_secret_hex: str, wake_id: str, envelope: dict[str, Any]) -> dict[str, Any]:
    """Verify HMAC-SHA256 tag before decrypting approval envelope from relay."""
    if not isinstance(envelope, dict) or int(envelope.get("v") or 0) != 2:
        raise ValueError("Unsupported encrypted approval envelope version.")
    key = bytes.fromhex(normalize_pair_secret_hex(pair_secret_hex))
    iv = base64.b64decode(envelope["nonce"])
    ct = base64.b64decode(envelope["ct"])
    tag = base64.b64decode(envelope["tag"])
    if len(iv) != 16 or len(tag) != 32:
        raise ValueError("Malformed nonce or authentication tag.")
    enc_key = hmac.new(key, b"enc-v2:" + iv, hashlib.sha256).digest()
    mac_key = hmac.new(key, b"mac-v2:" + iv, hashlib.sha256).digest()
    mac_input = b"wake-approve-v2|" + wake_id.encode("utf-8") + b"|" + iv + ct
    expected_tag = hmac.new(mac_key, mac_input, hashlib.sha256).digest()
    if not hmac.compare_digest(expected_tag, tag):
        raise ValueError("Authentication tag mismatch — approval payload integrity failed.")
    ks = _ctr_keystream(enc_key, iv, len(ct))
    plain = bytes(a ^ b for a, b in zip(ct, ks))
    return json.loads(plain.decode("utf-8"))


def _request_json(
    method: str,
    url: str,
    *,
    token: str | None = None,
    device_hash: str | None = None,
    payload: dict[str, Any] | None = None,
    timeout: int = 15,
) -> tuple[int, dict[str, Any]]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    if token:
        headers["X-Session-Token"] = token
    if device_hash:
        headers["X-Device-Hash"] = device_hash
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
        data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        try:
            return exc.code, json.loads(raw) if raw else {"ok": False, "error": str(exc)}
        except Exception:
            return exc.code, {"ok": False, "error": raw or str(exc)}
    except Exception as exc:
        return 0, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def request_biometric_wake(
    base_url: str,
    pair_secret_hex: str,
    *,
    tier: int = 1,
    duration_seconds: int = 1800,
    reason: str = "AI diagnostic & QA session",
    requester: str = "AI Assistant",
    wake_timeout: int = 90,
) -> tuple[bool, dict[str, Any]]:
    """Send signed wake request to background device and wait for biometric fingerprint approval."""
    norm_secret = normalize_pair_secret_hex(pair_secret_hex)
    dev_hash = compute_wake_channel_hash(norm_secret)
    wake_id = secrets.token_hex(12)
    ts = int(time.time())
    tier = max(0, min(2, int(tier)))
    duration_seconds = max(300, min(3600, int(duration_seconds)))
    clean_reason = (reason or "AI session request").strip()[:160]
    sig = sign_wake_request(norm_secret, wake_id, ts, tier, duration_seconds, clean_reason)

    code, data = _request_json(
        "POST",
        f"{base_url}/v1/wake/request",
        device_hash=dev_hash,
        payload={
            "wakeId": wake_id,
            "timestampUnix": ts,
            "requestedTier": tier,
            "durationSeconds": duration_seconds,
            "reason": clean_reason,
            "requester": (requester or "AI Assistant")[:64],
            "signature": sig,
        },
    )
    if code != 200 or not data.get("ok"):
        return False, {"error": f"Wake request rejected (HTTP {code}): {data.get('error', data)}"}

    print(
        f"[WAKE_SENT|id={wake_id}|tier=T{tier}|ttl={duration_seconds}s] Waiting for biometric fingerprint approval on device (up to {wake_timeout}s)...",
        file=sys.stderr,
    )
    deadline = time.monotonic() + max(10, wake_timeout)
    while time.monotonic() < deadline:
        time.sleep(2.0)
        s_code, s_data = _request_json(
            "GET",
            f"{base_url}/v1/wake/status?wakeId={urllib.parse.quote(wake_id)}",
            device_hash=dev_hash,
        )
        if s_code == 404:
            return False, {"error": "Wake request expired on relay before approval."}
        if s_code != 200:
            continue
        status = str(s_data.get("status") or "pending").lower()
        if status == "denied":
            return False, {"error": f"Device user denied wake request: {s_data.get('reason', 'denied')}"}
        if status == "approved":
            env_blob = s_data.get("encryptedSession")
            try:
                unlocked = decrypt_approval_payload(norm_secret, wake_id, env_blob)
                return True, unlocked
            except Exception as exc:
                return False, {"error": f"Failed to decrypt approved session: {exc}"}

    return False, {"error": f"Timed out after {wake_timeout}s waiting for biometric approval on device."}


EXIT_SESSION_EXPIRED = 4


def dispatch_command(
    base_url: str,
    session_token: str,
    cmd: str,
    *,
    args_obj: dict[str, Any] | None = None,
    lines: int | None = None,
    errors_only: bool = False,
    timeout: int = 45,
) -> tuple[bool, dict[str, Any]]:
    request_id = uuid.uuid4().hex[:16]
    payload: dict[str, Any] = {
        "requestId": request_id,
        "cmd": cmd.strip().lower(),
        "args": args_obj or {},
    }
    if lines is not None:
        payload["lines"] = max(20, min(1000, int(lines)))
    if errors_only:
        payload["errorsOnly"] = True

    code, data = _request_json("POST", f"{base_url}/v1/session/cmd", token=session_token, payload=payload)
    if code == 404:
        return False, {"error": "Session expired or closed on device.", "sessionExpired": True, "httpStatus": 404}
    if code != 200 or not data.get("ok"):
        return False, {"error": f"Failed queueing command (HTTP {code}): {data.get('error', data)}", "httpStatus": code}

    deadline = time.monotonic() + max(5, timeout)
    while time.monotonic() < deadline:
        time.sleep(1.2)
        r_code, r_data = _request_json(
            "GET",
            f"{base_url}/v1/session/result?requestId={urllib.parse.quote(request_id)}",
            token=session_token,
        )
        if r_code == 404:
            return False, {"error": "Session expired or closed on device.", "sessionExpired": True, "httpStatus": 404}
        if r_code == 200 and r_data.get("ready"):
            res = r_data.get("result", {})
            return bool(res.get("ok", True)), res

    return False, {"error": f"Device did not respond within {timeout}s.", "timedOut": True}


def main(
    argv: list[str] | None = None,
    *,
    env_url_vars: tuple[str, ...] = ("LIVE_BRIDGE_URL", "CAVERN_RELAY_URL"),
    env_secret_vars: tuple[str, ...] = ("LIVE_BRIDGE_PAIR_SECRET", "CAVERN_BRIDGE_PAIR_SECRET"),
    on_session_acquired: Any = None,
) -> int:
    parser = argparse.ArgumentParser(description="Live Device/App Bridge v3 client (ephemeral code + biometric wake + unrestricted dynamic Tier 2 commands).")
    parser.add_argument("--url", default="", help="HTTPS relay Worker URL (or set LIVE_BRIDGE_URL / CAVERN_RELAY_URL)")
    parser.add_argument("--code", default="", help="Active ephemeral session code")
    parser.add_argument("--wake", action="store_true", help="Send HMAC-signed wake request for biometric approval on standby device")
    parser.add_argument("--wake-only", action="store_true", help="Only wake & print ephemeral session metadata (do not run a command)")
    parser.add_argument("--pair-secret", default="", help="64-hex device pairing secret (or env LIVE_BRIDGE_PAIR_SECRET)")
    parser.add_argument("--tier", type=int, default=1, choices=[0, 1, 2], help="Requested session tier for --wake (0=Read, 1=Control, 2=Unrestricted Dynamic)")
    parser.add_argument("--duration", type=int, default=1800, help="Requested session TTL seconds for --wake (300..3600)")
    parser.add_argument("--reason", default="AI diagnostic & interactive QA", help="Human-readable reason shown on phone biometric prompt")
    parser.add_argument("--wake-timeout", type=int, default=90, help="Max seconds to wait for biometric approval")
    parser.add_argument("--cmd", default="ping", help="Command name (e.g. ping, capabilities, ultra_check, acoustic_audit, acoustic_probe, ui_perf_audit, ui_audit, reflect_types, reflect_members, reflect_get, reflect_set, reflect_invoke, ui_interact, shell_exec, pipeline, screenshot_ui)")
    parser.add_argument("--args", default="{}", help="JSON object of command arguments")
    parser.add_argument("--batch", default="", help="JSON array of {cmd, args} steps (shorthand for --cmd batch)")
    parser.add_argument("--pipeline", default="", help="JSON array of {cmd, args} steps with $prev result chaining (shorthand for --cmd pipeline, Tier 2)")
    parser.add_argument("--capabilities", action="store_true", help="Shortcut for --cmd capabilities")
    parser.add_argument("--lines", type=int, default=150, help="Lines for log_tail (20..1000)")
    parser.add_argument("--errors-only", action="store_true", help="Filter log_tail to WARN/ERROR/FATAL")
    parser.add_argument("--save-screenshot", default="", help="If command returns imageBase64, decode and save JPEG to this path")
    parser.add_argument("--timeout", type=int, default=60, help="Max seconds to wait for command response")
    args = parser.parse_args(argv)

    raw_url = args.url or next((os.environ.get(v, "").strip() for v in env_url_vars if os.environ.get(v, "").strip()), "")
    if not raw_url:
        print(f"ERROR: Provide --url or set one of {', '.join(env_url_vars)}.", file=sys.stderr)
        return 2
    try:
        base_url = validate_https_url(raw_url)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    token = args.code.strip()
    if args.wake or args.wake_only:
        secret = args.pair_secret or next((os.environ.get(v, "").strip() for v in env_secret_vars if os.environ.get(v, "").strip()), "")
        if not secret:
            print(f"ERROR: --wake requires --pair-secret or one of {', '.join(env_secret_vars)}.", file=sys.stderr)
            return 2
        ok, unlocked = request_biometric_wake(
            base_url,
            secret,
            tier=args.tier,
            duration_seconds=args.duration,
            reason=args.reason,
            wake_timeout=args.wake_timeout,
        )
        if not ok:
            print(f"ERROR: {unlocked.get('error', unlocked)}", file=sys.stderr)
            return 1
        token = str(unlocked.get("sessionCode") or "").strip()
        if not token:
            print("ERROR: Approved wake response missing sessionCode.", file=sys.stderr)
            return 1
        ttl_sec = int(unlocked.get("ttlSeconds") or args.duration or 1800)
        if callable(on_session_acquired):
            try:
                on_session_acquired(base_url, token, ttl_sec, unlocked)
            except Exception:
                pass
        print(
            f"[WAKE_APPROVED|tier=T{unlocked.get('tier')}|ttl={ttl_sec}s|device={unlocked.get('device', '?')}]",
            file=sys.stderr,
        )
        if args.wake_only:
            print(json.dumps(unlocked, indent=2, ensure_ascii=False))
            return 0

    if not token:
        print("ERROR: Provide --code <session-code> or --wake --pair-secret <hex>.", file=sys.stderr)
        return 2

    cmd = "capabilities" if args.capabilities else args.cmd
    try:
        args_obj = json.loads(args.args) if args.args else {}
        if not isinstance(args_obj, dict):
            raise ValueError("--args must be a JSON object.")
    except Exception as exc:
        print(f"ERROR parsing --args JSON: {exc}", file=sys.stderr)
        return 2

    if args.batch:
        try:
            steps = json.loads(args.batch)
            if not isinstance(steps, list):
                raise ValueError("--batch must be a JSON array of {cmd, args}.")
        except Exception as exc:
            print(f"ERROR parsing --batch JSON: {exc}", file=sys.stderr)
            return 2
        cmd = "batch"
        args_obj = {"commands": steps}
    elif args.pipeline:
        try:
            steps = json.loads(args.pipeline)
            if not isinstance(steps, list):
                raise ValueError("--pipeline must be a JSON array of {cmd, args}.")
        except Exception as exc:
            print(f"ERROR parsing --pipeline JSON: {exc}", file=sys.stderr)
            return 2
        cmd = "pipeline"
        args_obj = {"steps": steps}

    ok, res = dispatch_command(
        base_url,
        token,
        cmd,
        args_obj=args_obj,
        lines=args.lines,
        errors_only=bool(args.errors_only),
        timeout=args.timeout,
    )
    if "error" in res and "body" not in res:
        print(f"ERROR: {res['error']}", file=sys.stderr)
        return EXIT_SESSION_EXPIRED if res.get("sessionExpired") else 1

    print(f"=== LIVE DEVICE RESPONSE [{res.get('cmd', cmd)}] (ok={res.get('ok')}, tier=T{res.get('tier', '?')}, {res.get('elapsedMs', 0)} ms) ===")
    body = res.get("body", "")
    if args.save_screenshot and isinstance(body, str):
        try:
            parsed_body = json.loads(body)
            img_b64 = parsed_body.get("imageBase64") if isinstance(parsed_body, dict) else None
            if img_b64:
                out_p = Path(args.save_screenshot).expanduser()
                out_p.parent.mkdir(parents=True, exist_ok=True)
                out_p.write_bytes(base64.b64decode(img_b64))
                parsed_body = {k: (f"<saved {len(img_b64)} b64 chars -> {out_p}>" if k == "imageBase64" else v) for k, v in parsed_body.items()}
                body = json.dumps(parsed_body, indent=2, ensure_ascii=False)
        except Exception:
            pass

    if isinstance(body, str):
        print(body)
    else:
        print(json.dumps(body, indent=2, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
