#!/usr/bin/env python3
"""Terminal interface to monitor knapikette's messages and reply on its behalf."""
import socket
import sys
import threading
import readline
import os
import re
import subprocess
import tempfile
import shutil
import urllib.request
import urllib.error
try:
    import emoji as _emoji_lib
    def _resolve_emoji_shortcode(text: str) -> str:
        if text.startswith(':') and text.endswith(':') and len(text) > 2:
            converted = _emoji_lib.emojize(text, language='alias')
            if converted == text:
                converted = _emoji_lib.emojize(text)
            return converted
        return text
except ImportError:
    def _resolve_emoji_shortcode(text: str) -> str:
        return text

SOCKET_PATH = "/tmp/knapikette.sock"
IMAGE_URL_RE = re.compile(
    r'https?://(?:\S+?\.(?:gif|png|jpe?g|webp)(?:[?#]\S*)?'
    r'|(?:[\w.-]+\.)?kiply\.com/gifs/\S+'
    r'|media\.giphy\.com/media/\S+/giphy\.gif\S*'
    r'|c\.tenor\.com/\S+\.gif\S*)',
    re.IGNORECASE
)

current_channel_id = None
current_channel_name = None
current_guild_id = None
channels: dict[str, tuple[str, str]] = {}        # channel_id -> (name, guild_id)
members: dict[str, tuple[str, str]] = {}         # member_id -> (display_name, username)
member_status: dict[str, str] = {}               # member_id -> status (online/idle/dnd/offline)
state_lock = threading.Lock()
output_lock = threading.Lock()

msg_log: list[tuple[int, str, str, str, str]] = []  # (message_id, cid, cname, author, content)
msg_id_to_info: dict[int, tuple[int, str, str]] = {}  # message_id -> (counter, author, content)
msg_log_offset: int = 0
MAX_MSG_LOG = 500
show_images: bool = False
image_buffer: list[str] = []

STATUS_ICON = {
    "online":  "\033[1;32m●\033[0m",
    "idle":    "\033[1;33m●\033[0m",
    "dnd":     "\033[1;31m●\033[0m",
    "offline": "\033[2;37m○\033[0m",
}


def get_prompt() -> str:
    with state_lock:
        if current_channel_name:
            return f"\033[1;36m[#{current_channel_name}]\033[0m > "
        return "> "


def safe_print(msg: str):
    """Print without corrupting readline's in-progress input line."""
    with output_lock:
        buf = readline.get_line_buffer()
        prompt = get_prompt()
        sys.stdout.write(f"\r\033[K{msg}\n{prompt}{buf}")
        sys.stdout.flush()


def resolve_mentions(text: str) -> str:
    """Replace @name with <@id> for known members."""
    def replace(m):
        name = m.group(1)
        with state_lock:
            for mid, (display, username) in members.items():
                if display.lower() == name.lower() or username.lower() == name.lower():
                    return f"<@{mid}>"
        return m.group(0)
    return re.sub(r"@(\S+)", replace, text)


def render_image(url: str):
    if shutil.which("chafa") is None:
        safe_print("\033[1;33m[chafa non installé — installe-le avec : sudo apt install chafa]\033[0m")
        return
    tmp_path = None
    try:
        cols = shutil.get_terminal_size((80, 24)).columns
        with tempfile.NamedTemporaryFile(delete=False) as f:
            tmp_path = f.name
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            with open(tmp_path, "wb") as f:
                f.write(resp.read(5 * 1024 * 1024))
        result = subprocess.run(
            ["chafa", "--size", f"{cols}x20", tmp_path],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0 and result.stdout:
            safe_print(result.stdout.rstrip("\n"))
        elif result.stderr:
            safe_print(f"\033[2m[chafa : {result.stderr.strip()}]\033[0m")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            safe_print(f"\033[2m[Image 404 : lien expiré ou inaccessible]\033[0m")
        else:
            safe_print(f"\033[2m[Image non affichable : HTTP {e.code}]\033[0m")
    except Exception as e:
        safe_print(f"\033[2m[Image non affichable : {e}]\033[0m")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def format_message(channel_name: str, channel_id: str, author: str, content: str, counter: int) -> str:
    active = channel_id == current_channel_id
    ch = f"\033[1;32m#{channel_name}\033[0m" if active else f"\033[0;32m#{channel_name}\033[0m"
    tag = " \033[1;33m*\033[0m" if active else ""
    idx = f"\033[2m[{counter}]\033[0m "
    return f"{idx}{ch}{tag} \033[1;35m{author}\033[0m: {content}"


def receiver(sock: socket.socket):
    global current_channel_id, current_channel_name, current_guild_id, msg_log_offset
    buf = b""
    while True:
        try:
            chunk = sock.recv(4096)
        except OSError:
            safe_print("\033[1;31m[Déconnecté du bot]\033[0m")
            os._exit(0)
        if not chunk:
            safe_print("\033[1;31m[Bot a fermé la connexion]\033[0m")
            os._exit(0)
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            text = line.decode(errors="replace").strip()
            if not text:
                continue
            if text.startswith("CHANNEL|"):
                parts = text.split("|", 3)
                if len(parts) == 4:
                    _, cid, cname, gid = parts
                    with state_lock:
                        channels[cid] = (cname, gid)
                        if current_channel_id is None:
                            current_channel_id = cid
                            current_channel_name = cname
                            current_guild_id = gid
            elif text.startswith("MEMBER|"):
                parts = text.split("|", 4)
                if len(parts) == 5:
                    _, mid, display, username, _ = parts
                    with state_lock:
                        members[mid] = (display, username)
            elif text.startswith("PRESENCE|"):
                parts = text.split("|", 2)
                if len(parts) == 3:
                    _, mid, status = parts
                    with state_lock:
                        member_status[mid] = status
            elif text.startswith("MSG|"):
                parts = text.split("|", 6)
                if len(parts) >= 6:
                    _, cid, cname, author, mid_str, ref_id_str = parts[:6]
                    content = parts[6] if len(parts) == 7 else ""
                    try:
                        mid = int(mid_str)
                    except ValueError:
                        mid = 0
                    with state_lock:
                        if cid not in channels:
                            channels[cid] = (cname, "")
                        if current_channel_id is None:
                            current_channel_id = cid
                            current_channel_name = cname
                        if mid:
                            msg_log.append((mid, cid, cname, author, content))
                            if len(msg_log) > MAX_MSG_LOG:
                                evicted = msg_log.pop(0)
                                msg_id_to_info.pop(evicted[0], None)
                                msg_log_offset += 1
                            counter = len(msg_log) + msg_log_offset
                            msg_id_to_info[mid] = (counter, author, content)
                        else:
                            counter = 0
                        ref_display = ""
                        if ref_id_str:
                            try:
                                ref_info = msg_id_to_info.get(int(ref_id_str))
                                if ref_info:
                                    r_cnt, r_author, r_content = ref_info
                                    preview = r_content[:40] + ("…" if len(r_content) > 40 else "")
                                    ref_display = f"\n    \033[2m↩ [{r_cnt}] {r_author}: {preview}\033[0m"
                            except ValueError:
                                pass
                    safe_print(format_message(cname, cid, author, content, counter) + ref_display)
                    for img_url in IMAGE_URL_RE.findall(content):
                        image_buffer.append(img_url)
                        if show_images:
                            threading.Thread(target=render_image, args=(img_url,), daemon=True).start()
            elif text.startswith("IMAGE|"):
                parts = text.split("|", 3)
                if len(parts) == 4:
                    _, cid, mid_str, url = parts
                    image_buffer.append(url)
                    if show_images:
                        threading.Thread(target=render_image, args=(url,), daemon=True).start()
            elif text.startswith("REACTION_ADD|"):
                parts = text.split("|", 5)
                if len(parts) >= 5:
                    _, cid, cname, display, emoji = parts[:5]
                    mid_str = parts[5] if len(parts) == 6 else ""
                    with state_lock:
                        act = cid == current_channel_id
                        ref = ""
                        if mid_str:
                            try:
                                info = msg_id_to_info.get(int(mid_str))
                                if info:
                                    cnt, msg_author, msg_content = info
                                    preview = msg_content[:40] + ("…" if len(msg_content) > 40 else "")
                                    ref = f" \033[2m→ [{cnt}] {msg_author}: {preview}\033[0m"
                                else:
                                    ref = " \033[2m(msg ancien)\033[0m"
                            except ValueError:
                                pass
                    ch = f"\033[1;32m#{cname}\033[0m" if act else f"\033[0;32m#{cname}\033[0m"
                    safe_print(f"{ch} \033[1;35m{display}\033[0m a réagi {emoji}{ref}")
            elif text.startswith("REACTION_REMOVE|"):
                parts = text.split("|", 5)
                if len(parts) >= 5:
                    _, cid, cname, display, emoji = parts[:5]
                    mid_str = parts[5] if len(parts) == 6 else ""
                    with state_lock:
                        act = cid == current_channel_id
                        ref = ""
                        if mid_str:
                            try:
                                info = msg_id_to_info.get(int(mid_str))
                                if info:
                                    cnt, msg_author, msg_content = info
                                    preview = msg_content[:40] + ("…" if len(msg_content) > 40 else "")
                                    ref = f" \033[2m→ [{cnt}] {msg_author}: {preview}\033[0m"
                                else:
                                    ref = " \033[2m(msg ancien)\033[0m"
                            except ValueError:
                                pass
                    ch = f"\033[1;32m#{cname}\033[0m" if act else f"\033[0;32m#{cname}\033[0m"
                    safe_print(f"{ch} \033[1;35m{display}\033[0m a retiré {emoji}{ref}")
            elif text.startswith("REPLY|"):
                msg = text[6:]
                safe_print(f"\033[1;33m{msg}\033[0m")


def handle_command(line: str, sock: socket.socket):
    """Parse a /command and send CMD| to the bot."""
    global current_channel_id, current_channel_name, current_guild_id, show_images

    parts = line[1:].split(" ", 1)
    command = parts[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ""

    with state_lock:
        cid = current_channel_id
        gid = current_guild_id

    if command in ("channel",):
        target = args
        with state_lock:
            match = next(
                ((k, v[0], v[1]) for k, v in channels.items()
                 if v[0] == target or k == target),
                None
            )
            known = ", ".join(f"#{v[0]}" for v in channels.values()) or "aucun"
        if match:
            with state_lock:
                current_channel_id = match[0]
                current_channel_name = match[1]
                current_guild_id = match[2]
            safe_print(f"[Basculé vers \033[1;32m#{match[1]}\033[0m]")
        else:
            safe_print(f"[Salon inconnu : {target}  —  connus : {known}]")
        return

    if command in ("channels",):
        with state_lock:
            chans = list(channels.items())
            active = current_channel_id
        if not chans:
            safe_print("[Aucun salon connu]")
        else:
            for cid_k, (cname, _) in chans:
                marker = " \033[1;33m← actif\033[0m" if cid_k == active else ""
                safe_print(f"  \033[0;32m#{cname}\033[0m  ({cid_k}){marker}")
        return

    if command == "help":
        safe_print(
            "\033[1mCommandes Discord :\033[0m\n"
            "  /clear_history          — effacer l'historique du salon actif\n"
            "  /shut_up                — silencier le bot\n"
            "  /unshut_up              — réactiver le bot\n"
            "  /list_personalities     — lister les personnalités\n"
            "  /use_personality <nom>  — changer de personnalité\n"
            "  /reply <N> <message>    — répondre au message [N]\n"
            "  /react <N> <emoji>      — réagir au message [N] (ex: /react 3 :thumbsup: ou :rofl:)\n"
            "  /img                    — activer/désactiver l'affichage des images (désactivé par défaut)\n"
            "\033[1mCommandes locales :\033[0m\n"
            "  /channel <nom>          — changer de salon actif\n"
            "  /channels               — lister les salons connus\n"
            "  /members                — lister les membres (avec statut)\n"
            "  /online                 — lister les membres en ligne\n"
            "  /quit                   — quitter\n"
            "\033[1mEnvoyer :\033[0m tapez un message + Entrée\n"
            "  @pseudo       — mention individuelle\n"
            "  @everyone     — mention tout le monde (permission requise)\n"
            "  @here         — mention membres en ligne"
        )
        return

    if command == "members":
        with state_lock:
            mems = sorted(
                [(mid, *v) for mid, v in members.items()],
                key=lambda x: x[1].lower()
            )
            statuses = dict(member_status)
        if not mems:
            safe_print("[Aucun membre connu]")
        else:
            for mid, display, username in mems:
                status = statuses.get(mid, "offline")
                icon = STATUS_ICON.get(status, STATUS_ICON["offline"])
                safe_print(f"  {icon} \033[1;35m{display}\033[0m (@{username})")
        return

    if command == "online":
        with state_lock:
            mems = [
                (mid, display, username)
                for mid, (display, username) in members.items()
                if member_status.get(mid, "offline") != "offline"
            ]
            mems.sort(key=lambda x: x[1].lower())
            statuses = dict(member_status)
        if not mems:
            safe_print("[Personne en ligne]")
        else:
            for mid, display, username in mems:
                status = statuses.get(mid, "offline")
                icon = STATUS_ICON.get(status, STATUS_ICON["offline"])
                safe_print(f"  {icon} \033[1;35m{display}\033[0m (@{username})  \033[2m{status}\033[0m")
        return

    if command == "img":
        show_images = not show_images
        if show_images:
            imgs = list(image_buffer)
            if imgs:
                def _render_all(urls):
                    for u in urls:
                        render_image(u)
                threading.Thread(target=_render_all, args=(imgs,), daemon=True).start()
            state = "\033[1;32mactivé\033[0m"
        else:
            with state_lock:
                log_copy = list(msg_log)
                offset = msg_log_offset
            lines = [
                format_message(m_cname, m_cid, author, content, i + 1 + offset)
                for i, (mid, m_cid, m_cname, author, content) in enumerate(log_copy)
            ]
            with output_lock:
                buf = readline.get_line_buffer()
                prompt = get_prompt()
                combined = "\n".join(lines)
                sys.stdout.write(f"\033[2J\033[H{combined}\n{prompt}{buf}")
                sys.stdout.flush()
            state = "\033[2mdésactivé\033[0m"
        safe_print(f"[Affichage des images {state}]")
        return

    if command in ("quit", "exit"):
        print("\n[Déconnecté]")
        os._exit(0)

    if command == "reply":
        parts_args = args.split(" ", 1)
        if not parts_args[0].isdigit() or len(parts_args) < 2:
            safe_print("[Usage : /reply <N> <message>]")
            return
        n = int(parts_args[0])
        text_to_send = parts_args[1]
        with state_lock:
            idx = n - msg_log_offset - 1
            entry = msg_log[idx] if 0 <= idx < len(msg_log) else None
        if entry is None:
            safe_print(f"[Message [{n}] introuvable]")
            return
        mid, r_cid, r_cname, orig_author, orig_content = entry
        resolved = resolve_mentions(text_to_send)
        try:
            sock.sendall(f"REPLY_TO|{r_cid}|{mid}|{resolved}\n".encode())
            preview = orig_content[:40] + ("…" if len(orig_content) > 40 else "")
            safe_print(f"\033[0;33m[Réponse → #{r_cname} | [{n}] {orig_author}: {preview}]\033[0m {resolved}")
        except OSError as e:
            safe_print(f"\033[1;31m[Erreur d'envoi : {e}]\033[0m")
        return

    if command == "react":
        parts_args = args.split(" ", 1)
        if not parts_args[0].isdigit() or len(parts_args) < 2:
            safe_print("[Usage : /react <N> <emoji>]")
            return
        n = int(parts_args[0])
        emoji = _resolve_emoji_shortcode(parts_args[1].strip())
        with state_lock:
            idx = n - msg_log_offset - 1
            entry = msg_log[idx] if 0 <= idx < len(msg_log) else None
        if entry is None:
            safe_print(f"[Message [{n}] introuvable]")
            return
        mid, r_cid, r_cname, _, _ = entry
        try:
            sock.sendall(f"REACT|{r_cid}|{mid}|{emoji}\n".encode())
            safe_print(f"\033[0;33m[Réaction {emoji} → [{n}]]\033[0m")
        except OSError as e:
            safe_print(f"\033[1;31m[Erreur d'envoi : {e}]\033[0m")
        return

    # Commands routed to the bot via CMD|
    if command == "clear_history":
        if not cid:
            safe_print("[Aucun salon actif]")
            return
        sock.sendall(f"CMD|clear_history|{cid}\n".encode())
    elif command == "shut_up":
        sock.sendall(b"CMD|shut_up\n")
    elif command == "unshut_up":
        sock.sendall(b"CMD|unshut_up\n")
    elif command == "list_personalities":
        sock.sendall(f"CMD|list_personalities|{gid or 0}\n".encode())
    elif command == "use_personality":
        if not args:
            safe_print("[Usage : /use_personality <nom>]")
            return
        sock.sendall(f"CMD|use_personality|{gid or 0}|{args}\n".encode())
    else:
        safe_print(f"[Commande inconnue : /{command}  —  /help pour l'aide]")


def main():
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(SOCKET_PATH)
    except FileNotFoundError:
        print(f"[Erreur] Socket introuvable : {SOCKET_PATH}\nLe bot tourne-t-il ?")
        sys.exit(1)
    except ConnectionRefusedError:
        print(f"[Erreur] Connexion refusée : {SOCKET_PATH}")
        sys.exit(1)

    print(
        "\033[1;32m[Connecté à knapikette]\033[0m  "
        "/help pour l'aide  |  @pseudo pour mentionner  |  /channel <nom> pour changer de salon"
    )

    t = threading.Thread(target=receiver, args=(sock,), daemon=True)
    t.start()

    readline.parse_and_bind("tab: complete")

    while True:
        try:
            line = input(get_prompt())
        except (EOFError, KeyboardInterrupt):
            print("\n[Déconnecté]")
            break

        line = line.strip()
        if not line:
            continue

        if line.startswith("/"):
            handle_command(line, sock)
            continue

        with state_lock:
            cid = current_channel_id
            cname = current_channel_name

        if cid is None:
            safe_print("[Aucun salon actif. Attendez un message ou utilisez /channel <nom>]")
            continue

        resolved = resolve_mentions(line)
        try:
            sock.sendall(f"SEND|{cid}|{resolved}\n".encode())
            display = resolved if resolved == line else f"{resolved}  \033[2m(→ {line})\033[0m"
            safe_print(f"\033[0;33m[Envoyé → #{cname}]\033[0m {display}")
        except OSError as e:
            safe_print(f"\033[1;31m[Erreur d'envoi : {e}]\033[0m")

    sock.close()


if __name__ == "__main__":
    main()
