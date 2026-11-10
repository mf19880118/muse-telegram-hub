#!/usr/bin/env python3
"""Telegram Bot API helper for the custom.telegram-bot connector in Muse.

The bot token never appears in this file. The URL is built with a
credential surrogate from Muse's dynamic_credentials helper, and the
real token is swapped in by the Muse runtime only on the outbound
request to api.telegram.org. Build the URL with the RAW surrogate, as
below: the swap matches it literally, so it must not be
percent-encoded. Never print request URLs (they carry the surrogate).
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")
from dynamic_credentials import dynamic_credential_entry, read_json_response

# Must match the connector id you registered in Muse.
CREDENTIAL = "custom.telegram-bot"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise RuntimeError("redirect refused, verify the endpoint")


def call(method: str, params: dict, http_timeout: float = 40.0) -> dict:
    # Build the URL with the RAW surrogate in the path. Do not use
    # url_with_surrogate_path_segment here: it percent-encodes the
    # surrogate's colon, the egress swap then cannot find the surrogate,
    # and Telegram answers 404. Raw "hsurr:..." in the path works; the
    # swap replaces it with the real token on the way out.
    surrogate = str(dynamic_credential_entry(CREDENTIAL)["surrogate"]).strip()
    url = "https://api.telegram.org/bot" + surrogate + "/" + method
    body = json.dumps(params).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(
            request, timeout=http_timeout
        ) as response:
            return read_json_response(response)
    except urllib.error.HTTPError as exc:
        try:
            return read_json_response(exc)
        except Exception:
            return {"ok": False, "error_code": exc.code, "description": str(exc)}


def call_multipart(method: str, fields: dict, file_param: str,
                   file_path: str, http_timeout: float = 60.0) -> dict:
    """POST a multipart/form-data request (e.g. sendPhoto with a local file).

    Same URL/surrogate handling as call(); the file is read from disk and
    never logged.
    """
    import mimetypes
    surrogate = str(dynamic_credential_entry(CREDENTIAL)["surrogate"]).strip()
    url = "https://api.telegram.org/bot" + surrogate + "/" + method
    boundary = "----muse-" + uuid.uuid4().hex
    body = b""
    for key, value in fields.items():
        if value is None:
            continue
        body += ("--" + boundary + "\r\n").encode()
        body += ('Content-Disposition: form-data; name="%s"\r\n\r\n' % key).encode()
        body += str(value).encode("utf-8") + b"\r\n"
    filename = os.path.basename(file_path)
    ctype = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
    with open(file_path, "rb") as fh:
        data = fh.read()
    body += ("--" + boundary + "\r\n").encode()
    body += ('Content-Disposition: form-data; name="%s"; filename="%s"\r\n'
             % (file_param, filename)).encode()
    body += ("Content-Type: %s\r\n\r\n" % ctype).encode()
    body += data + b"\r\n"
    body += ("--" + boundary + "--\r\n").encode()
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type",
                       "multipart/form-data; boundary=" + boundary)
    try:
        with urllib.request.build_opener(NoRedirect()).open(
            request, timeout=http_timeout
        ) as response:
            return read_json_response(response)
    except urllib.error.HTTPError as exc:
        try:
            return read_json_response(exc)
        except Exception:
            return {"ok": False, "error_code": exc.code, "description": str(exc)}


def send_chat_action(chat_id, action: str) -> dict:
    """Best-effort chat action (typing, upload_photo, ...); never raises."""
    try:
        return call("sendChatAction",
                    {"chat_id": chat_id, "action": action})
    except Exception:
        return {"ok": False}


def main() -> int:
    parser = argparse.ArgumentParser(description="Telegram Bot API helper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("get-me")

    p_upd = sub.add_parser("get-updates")
    p_upd.add_argument("--offset", type=int, default=None)
    p_upd.add_argument("--timeout", type=int, default=0)
    p_upd.add_argument("--limit", type=int, default=100)

    p_send = sub.add_parser("send-message")
    p_send.add_argument("--chat-id", required=True)
    p_send.add_argument("--text", required=True)
    p_send.add_argument("--reply-to", type=int, default=None)
    p_send.add_argument("--thread-id", type=int, default=None)
    p_send.add_argument("--typing", dest="typing", action="store_true",
                        default=True,
                        help="send a typing chat action just before the "
                             "message (default on)")
    p_send.add_argument("--no-typing", dest="typing", action="store_false",
                        help="skip the typing chat action")

    p_photo = sub.add_parser("send-photo")
    p_photo.add_argument("--chat-id", required=True)
    p_photo.add_argument("--photo", required=True,
                         help="local image file to upload")
    p_photo.add_argument("--caption", default=None)
    p_photo.add_argument("--reply-to", type=int, default=None)
    p_photo.add_argument("--thread-id", type=int, default=None)
    p_photo.add_argument("--no-upload-action", dest="upload_action",
                         action="store_false", default=True,
                         help="skip the upload_photo chat action first")

    p_act = sub.add_parser("chat-action")
    p_act.add_argument("--chat-id", required=True)
    p_act.add_argument("--action", default="typing",
                       help="e.g. typing, upload_photo, record_video")

    p_react = sub.add_parser("react")
    p_react.add_argument("--chat-id", required=True)
    p_react.add_argument("--message-id", type=int, required=True)
    p_react.add_argument("--emoji", default="👀",
                         help="reaction emoji, default 👀 (read/seen)")

    p_chat = sub.add_parser("get-chat")
    p_chat.add_argument("--chat-id", required=True)

    p_wh = sub.add_parser("set-webhook")
    p_wh.add_argument("--url-file", required=True,
                      help="file holding the tunnel base URL, no trailing slash")
    p_wh.add_argument("--webhook-config",
                      default=os.environ.get(
                          "TG_WEBHOOK_CONFIG",
                          os.path.expanduser(
                              "~/workspace/telegram-relay/webhook.json")),
                      help="JSON file with path and secret for the receiver")

    sub.add_parser("get-webhook-info")
    sub.add_parser("delete-webhook")

    args = parser.parse_args()

    if args.cmd == "get-me":
        result = call("getMe", {})
    elif args.cmd == "get-updates":
        params = {"timeout": args.timeout, "limit": args.limit,
                  "allowed_updates": ["message", "edited_message", "my_chat_member"]}
        if args.offset is not None:
            params["offset"] = args.offset
        result = call("getUpdates", params, http_timeout=args.timeout + 30)
    elif args.cmd == "send-message":
        if args.typing:
            # Typing shows for ~5s; pause so it is actually visible
            # before the message lands (was ~1s, too easy to miss).
            res = send_chat_action(args.chat_id, "typing")
            if res.get("ok"):
                time.sleep(3)
        params = {"chat_id": args.chat_id, "text": args.text}
        if args.reply_to is not None:
            params["reply_parameters"] = {"message_id": args.reply_to}
        if args.thread_id is not None:
            params["message_thread_id"] = args.thread_id
        result = call("sendMessage", params)
    elif args.cmd == "send-photo":
        if args.upload_action:
            # Shows "sending a photo..." while the file uploads, so the
            # user sees activity instead of silence.
            send_chat_action(args.chat_id, "upload_photo")
        fields = {"chat_id": args.chat_id}
        if args.caption is not None:
            fields["caption"] = args.caption
        if args.reply_to is not None:
            fields["reply_parameters"] = json.dumps(
                {"message_id": args.reply_to}, ensure_ascii=False)
        if args.thread_id is not None:
            fields["message_thread_id"] = args.thread_id
        result = call_multipart("sendPhoto", fields, "photo", args.photo)
    elif args.cmd == "chat-action":
        result = call("sendChatAction",
                      {"chat_id": args.chat_id, "action": args.action})
    elif args.cmd == "react":
        result = call("setMessageReaction", {
            "chat_id": args.chat_id,
            "message_id": args.message_id,
            "reaction": [{"type": "emoji", "emoji": args.emoji}],
        })
    elif args.cmd == "get-chat":
        result = call("getChat", {"chat_id": args.chat_id})
    elif args.cmd == "set-webhook":
        with open(args.url_file, "r", encoding="utf-8") as fh:
            base_url = fh.read().strip().rstrip("/")
        with open(args.webhook_config, "r", encoding="utf-8") as fh:
            wh = json.load(fh)
        result = call("setWebhook", {
            "url": base_url + "/" + wh["path"],
            "secret_token": wh["secret"],
            "allowed_updates": ["message", "edited_message", "my_chat_member"],
            "drop_pending_updates": False,
        })
    elif args.cmd == "get-webhook-info":
        result = call("getWebhookInfo", {})
    elif args.cmd == "delete-webhook":
        result = call("deleteWebhook", {"drop_pending_updates": False})
    else:  # pragma: no cover
        return 2

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
