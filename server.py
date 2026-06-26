#!/usr/bin/env python3
import base64
import hashlib
import hmac
import html
import json
import mimetypes
import os
import secrets
import shutil
import sqlite3
import string
import uuid
from email import policy
from email.parser import BytesParser
from http import cookies
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from io import BytesIO
from pathlib import Path
from urllib import error as urlerror
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
LOGO_DIR = ROOT / "logo"
DB_PATH = DATA_DIR / "app.db"
SESSION_BYTES = 32
PASSWORD_ITERATIONS = 390000
ADMIN_PASSPHRASE = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(22))
ADMIN_SESSIONS = set()
LIVE_WINDOW_MINUTES = 5
MAILJET_ENDPOINT = "https://api.mailjet.com/v3.1/send"
MAILJET_API_KEY = "4511561ef81c021c2ac39432a103c6b9"
MAILJET_SECRET_KEY = "230748ca596ab8f19a93a45f63916c14"
ADMIN_CODE_EMAIL_TO = "camille.decamps@orange.fr"
ADMIN_CODE_EMAIL_FROM = "albanczn@gmail.com"
FAVICON_FILES = {
    "android-chrome-192x192.png",
    "android-chrome-512x512.png",
    "apple-touch-icon.png",
    "favicon-16x16.png",
    "favicon-32x32.png",
    "favicon.ico",
    "site.webmanifest",
}
BOT_UA_MARKERS = (
    "bot",
    "crawl",
    "spider",
    "slurp",
    "ahrefs",
    "semrush",
    "facebookexternalhit",
    "bingpreview",
    "telegrambot",
    "discordbot",
    "twitterbot",
    "linkedinbot",
    "whatsapp",
    "curl",
    "wget",
    "python-requests",
    "httpx",
    "headless",
)


class MultipartField:
    def __init__(self, value="", filename="", content_type="", data=b""):
        self.value = value
        self.filename = filename
        self.type = content_type
        self.file = BytesIO(data)


PRODUCT_TYPES = ["Bouquet", "Composition", "Plantes"]
OCCASIONS = [
    "Décès",
    "Baptême",
    "Fête des mères",
    "Fête des pères",
    "Fête des écoles",
    "Naissance",
]
KNOWN_COLORS = [
    ("Blanc", "#F8F7F1"),
    ("Crème", "#E8DCC5"),
    ("Beige", "#D8C4A3"),
    ("Rose", "#D99AA5"),
    ("Rouge", "#8F2F2F"),
    ("Bordeaux", "#5B1F2E"),
    ("Orange", "#D88B4A"),
    ("Jaune", "#E9CF75"),
    ("Vert", "#5C6656"),
    ("Bleu", "#5B7C99"),
    ("Violet", "#796193"),
    ("Mauve", "#A98FBD"),
    ("Naturel", "#B49B74"),
]


def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    DATA_DIR.mkdir(exist_ok=True)
    UPLOAD_DIR.mkdir(exist_ok=True)
    with db() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE,
                first_name TEXT NOT NULL DEFAULT '',
                last_name TEXT NOT NULL DEFAULT '',
                address TEXT NOT NULL DEFAULT '',
                is_admin INTEGER NOT NULL DEFAULT 0,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS user_wishlist (
                user_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, product_id),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                category TEXT NOT NULL,
                product_types TEXT NOT NULL DEFAULT '[]',
                occasions TEXT NOT NULL DEFAULT '[]',
                price REAL NOT NULL,
                colors TEXT NOT NULL,
                photos TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS site_visitors (
                visitor_key TEXT PRIMARY KEY,
                first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                user_agent TEXT NOT NULL DEFAULT '',
                last_path TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS site_pageviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                visitor_key TEXT NOT NULL,
                path TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (visitor_key) REFERENCES site_visitors(visitor_key) ON DELETE CASCADE
            );
            """
        )
        product_columns = {row["name"] for row in conn.execute("PRAGMA table_info(products)").fetchall()}
        if "product_types" not in product_columns:
            conn.execute("ALTER TABLE products ADD COLUMN product_types TEXT NOT NULL DEFAULT '[]'")
        if "occasions" not in product_columns:
            conn.execute("ALTER TABLE products ADD COLUMN occasions TEXT NOT NULL DEFAULT '[]'")
        user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "first_name" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN first_name TEXT NOT NULL DEFAULT ''")
        if "last_name" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN last_name TEXT NOT NULL DEFAULT ''")
        if "address" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN address TEXT NOT NULL DEFAULT ''")
        if "is_admin" not in user_columns:
            conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
        admin = conn.execute("SELECT id FROM users WHERE email = 'admin'").fetchone()
        if not admin:
            password_hash, salt = hash_password(ADMIN_PASSPHRASE)
            conn.execute(
                """
                INSERT INTO users (email, first_name, last_name, address, is_admin, password_hash, salt)
                VALUES ('admin', 'Admin', 'Le Marais Fleuri', '', 1, ?, ?)
                """,
                (password_hash, salt),
            )
        else:
            password_hash, salt = hash_password(ADMIN_PASSPHRASE)
            conn.execute(
                "UPDATE users SET is_admin = 1, password_hash = ?, salt = ? WHERE email = 'admin'",
                (password_hash, salt),
            )


def hash_password(password, salt=None):
    raw_salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        raw_salt,
        PASSWORD_ITERATIONS,
    )
    return (
        base64.b64encode(digest).decode("ascii"),
        base64.b64encode(raw_salt).decode("ascii"),
    )


def verify_password(password, stored_hash, stored_salt):
    raw_salt = base64.b64decode(stored_salt.encode("ascii"))
    candidate, _ = hash_password(password, raw_salt)
    return hmac.compare_digest(candidate, stored_hash)


def esc(value):
    return html.escape(str(value), quote=True)


def json_list(value):
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def normalize_key(value):
    return (
        str(value)
        .strip()
        .lower()
        .replace("è", "e")
        .replace("é", "e")
        .replace("ê", "e")
        .replace("ë", "e")
        .replace("à", "a")
        .replace("â", "a")
        .replace("î", "i")
        .replace("ï", "i")
        .replace("ô", "o")
        .replace("û", "u")
        .replace("ù", "u")
        .replace("ç", "c")
    )


def canonical_values(values, options):
    mapping = {normalize_key(option): option for option in options}
    aliases = {
        "bouquets": "Bouquet",
        "compositions": "Composition",
        "plante": "Plantes",
        "plantes": "Plantes",
    }
    result = []
    for value in values:
        key = normalize_key(value)
        label = mapping.get(key) or aliases.get(key) or str(value).strip()
        if label and label not in result:
            result.append(label)
    return result


def json_response(handler, payload, status=200):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def send_admin_passphrase_email():
    auth_token = base64.b64encode(f"{MAILJET_API_KEY}:{MAILJET_SECRET_KEY}".encode("utf-8")).decode("ascii")
    payload = {
        "Messages": [
            {
                "From": {"Email": ADMIN_CODE_EMAIL_FROM, "Name": "Le Marais Fleuri"},
                "To": [{"Email": ADMIN_CODE_EMAIL_TO}],
                "Subject": "VOICI LE CODE DE CONNEXION ADMIN",
                "HTMLPart": f"<p>code: <strong>{esc(ADMIN_PASSPHRASE)}</strong>!</p>",
            }
        ]
    }
    request = Request(
        MAILJET_ENDPOINT,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Basic {auth_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            response.read()
        print(f"Code admin envoyé par email à {ADMIN_CODE_EMAIL_TO}.")
    except urlerror.HTTPError as exc:
        try:
            details = exc.read().decode("utf-8", "replace")
        except OSError:
            details = ""
        print(f"Email admin non envoyé: HTTP {exc.code} {exc.reason}. {details}")
    except (urlerror.URLError, TimeoutError, OSError) as exc:
        print(f"Email admin non envoyé: {exc}")


def read_products():
    with db() as conn:
        rows = conn.execute("SELECT * FROM products ORDER BY created_at DESC, id DESC").fetchall()
    products = []
    for row in rows:
        product_types = canonical_values(json_list(row["product_types"]), PRODUCT_TYPES)
        if not product_types and row["category"]:
            product_types = canonical_values([row["category"]], PRODUCT_TYPES)
        colors = canonical_values(json_list(row["colors"]), [label for label, _ in KNOWN_COLORS])
        occasions = canonical_values(json_list(row["occasions"]), OCCASIONS)
        products.append(
            {
                "id": row["id"],
                "url": f"/produit/{row['id']}",
                "name": row["name"],
                "description": row["description"],
                "category": row["category"],
                "types": product_types,
                "occasions": occasions,
                "colors": colors,
                "photos": json_list(row["photos"]),
            }
        )
    return products


def read_product(product_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if not row:
        return None
    product_types = canonical_values(json_list(row["product_types"]), PRODUCT_TYPES)
    if not product_types and row["category"]:
        product_types = canonical_values([row["category"]], PRODUCT_TYPES)
    colors = canonical_values(json_list(row["colors"]), [label for label, _ in KNOWN_COLORS])
    occasions = canonical_values(json_list(row["occasions"]), OCCASIONS)
    return {
        "id": row["id"],
        "url": f"/produit/{row['id']}",
        "name": row["name"],
        "description": row["description"],
        "category": row["category"],
        "types": product_types,
        "occasions": occasions,
        "colors": colors,
        "photos": json_list(row["photos"]),
    }


def public_products():
    return read_products()


def read_wishlist_ids(user_id):
    with db() as conn:
        rows = conn.execute(
            "SELECT product_id FROM user_wishlist WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    return [str(row["product_id"]) for row in rows]


def favicon_links():
    return """
    <link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">
    <link rel="icon" type="image/png" sizes="32x32" href="/favicon-32x32.png">
    <link rel="icon" type="image/png" sizes="16x16" href="/favicon-16x16.png">
    <link rel="icon" href="/favicon.ico" sizes="any">
    <link rel="manifest" href="/site.webmanifest">
    <meta name="theme-color" content="#F6F7F4">
    """


def is_bot_user_agent(user_agent):
    agent = (user_agent or "").lower()
    if not agent:
        return True
    return any(marker in agent for marker in BOT_UA_MARKERS)


def analytics_snapshot():
    with db() as conn:
        summary = {
            "live_clients": conn.execute(
                "SELECT COUNT(*) AS count FROM site_visitors WHERE last_seen >= datetime('now', ?)",
                (f"-{LIVE_WINDOW_MINUTES} minutes",),
            ).fetchone()["count"],
            "total_clients": conn.execute("SELECT COUNT(*) AS count FROM site_visitors").fetchone()["count"],
            "pageviews": conn.execute("SELECT COUNT(*) AS count FROM site_pageviews").fetchone()["count"],
            "registered_clients": conn.execute("SELECT COUNT(*) AS count FROM users WHERE is_admin = 0").fetchone()["count"],
            "wishlist_total": conn.execute("SELECT COUNT(*) AS count FROM user_wishlist").fetchone()["count"],
        }
        product_rows = conn.execute(
            """
            SELECT products.id, products.name, products.product_types, products.occasions,
                   COUNT(user_wishlist.product_id) AS wishlist_count
            FROM products
            LEFT JOIN user_wishlist ON user_wishlist.product_id = products.id
            GROUP BY products.id
            ORDER BY wishlist_count DESC, products.created_at DESC, products.id DESC
            """
        ).fetchall()
        recent_rows = conn.execute(
            """
            SELECT path, COUNT(*) AS views
            FROM site_pageviews
            WHERE created_at >= datetime('now', '-7 days')
            GROUP BY path
            ORDER BY views DESC, path ASC
            LIMIT 8
            """
        ).fetchall()
    return summary, product_rows, recent_rows


def shell():
    return """
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    """ + favicon_links() + """
    <link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,300;0,400;0,500;0,600;1,300;1,400;1,500;1,600&family=Montserrat:wght@200;300;400;500;600&display=swap" rel="stylesheet">
    <script src="https://cdn.tailwindcss.com"></script>
    <script>
      tailwind.config = {
        theme: {
          extend: {
            colors: { brand: { green: '#5C6656', gold: '#C5A880' } },
            fontFamily: {
              serif: ['"Cormorant Garamond"', 'serif'],
              sans: ['"Montserrat"', 'sans-serif']
            }
          }
        }
      }
    </script>
    <script src="https://unpkg.com/lucide@latest"></script>
    <style>
      ::selection { background-color: #5C6656; color: white; }
      body { background-color: #F6F7F4; color: #2C3328; }
      ::-webkit-scrollbar { width: 8px; }
      ::-webkit-scrollbar-track { background: #F6F7F4; }
      ::-webkit-scrollbar-thumb { background: #5C6656; border-radius: 4px; }
      ::-webkit-scrollbar-thumb:hover { background: #2C3328; }
      .leaf-shape { border-radius: 0 50% 0 50%; }
      .flower-petal { border-radius: 50% 50% 0 50%; }
      @keyframes float {
        0% { transform: translateY(0) rotate(var(--rot, 0deg)); }
        50% { transform: translateY(-16px) rotate(calc(var(--rot, 0deg) + 4deg)); }
        100% { transform: translateY(0) rotate(var(--rot, 0deg)); }
      }
      .float-anim { animation: float 11s ease-in-out infinite; }
      .field {
        width: 100%;
        border: 1px solid rgba(92,102,86,.24);
        background: rgba(255,255,255,.72);
        padding: 14px 16px;
        outline: none;
        transition: border-color .2s, box-shadow .2s;
      }
      .field:focus {
        border-color: #5C6656;
        box-shadow: 0 0 0 4px rgba(92,102,86,.12);
      }
      .btn-dark {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        gap: 12px;
        border: 1px solid #2C3328;
        background: #2C3328;
        color: #F6F7F4;
        padding: 14px 22px;
        text-transform: uppercase;
        letter-spacing: .18em;
        font-size: 11px;
        font-weight: 500;
      }
      .btn-light {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        gap: 12px;
        border: 1px solid rgba(44,51,40,.28);
        color: #2C3328;
        padding: 12px 18px;
        text-transform: uppercase;
        letter-spacing: .16em;
        font-size: 10px;
        font-weight: 500;
      }
    </style>
    """


def nav(active=""):
    links = [
        ("/", "Menu"),
        ("/#propos", "À propos"),
        ("/#mariage", "Événements"),
        ("/client", "Marketplace"),
        ("/compte", "Compte"),
    ]
    items = []
    for href, label in links:
        color = "text-brand-gold" if active == label else "text-[#5C6656]"
        items.append(
            f"""
            <a href="{href}" class="hidden lg:block relative group py-1 {color}">
              <span class="group-hover:text-brand-gold transition-colors duration-300">{label}</span>
              <span class="absolute bottom-0 left-0 w-full h-[1px] bg-brand-gold transform origin-right scale-x-0 transition-transform duration-300 ease-out group-hover:scale-x-100 group-hover:origin-left"></span>
            </a>
            """
        )
    return f"""
    <nav id="navbar" class="fixed top-0 left-0 right-0 z-50 bg-[#F6F7F4]/90 backdrop-blur-md py-4 px-6 lg:px-12 flex justify-between items-center transition-all duration-300 border-b border-[#2C3328]/5">
      <div class="w-1/3 flex items-center">
        <a href="/" class="font-serif text-xl sm:text-2xl text-[#2C3328] font-medium tracking-wide">Le Marais Fleuri</a>
      </div>
      <div class="w-1/3 flex justify-center">
        <a href="/">
          <img src="https://raw.githubusercontent.com/ssbagpcm/files/refs/heads/main/ba62f9f2-a489-4539-99a6-e1c8fdf70e42_removalai_preview.png" alt="Le Marais Fleuri Logo" class="h-16 sm:h-20 md:h-24 mix-blend-multiply transition-all duration-300" />
        </a>
      </div>
      <div class="w-1/3 flex justify-end items-center gap-8 text-[10px] sm:text-xs font-sans uppercase tracking-[0.15em] font-medium">
        {''.join(items)}
        <a href="/compte" class="lg:hidden btn-light !px-3 !py-2"><i data-lucide="user" class="w-4 h-4"></i></a>
      </div>
    </nav>
    """


def page(title, body, active=""):
    return f"""<!DOCTYPE html>
<html lang="fr" class="scroll-smooth">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{esc(title)} - Le Marais Fleuri</title>
  {shell()}
</head>
<body class="font-sans antialiased overflow-x-hidden relative">
  <div class="absolute top-24 left-10 w-64 h-64 bg-brand-green/10 leaf-shape -z-10 blur-3xl float-anim" style="--rot:-12deg"></div>
  <div class="absolute top-52 right-16 w-32 h-32 bg-brand-green/15 flower-petal -z-10 blur-xl float-anim" style="--rot:45deg"></div>
  {nav(active)}
  {body}
  <script>
    lucide.createIcons();
    const LMF_STORE = {{
      authenticated: false,
      wishlistIds: new Set(),
      loadWishlist() {{
        return fetch('/api/wishlist')
          .then(response => response.ok ? response.json() : {{ authenticated: false, ids: [] }})
          .then(payload => {{
            this.authenticated = Boolean(payload.authenticated);
            this.wishlistIds = new Set((payload.ids || []).map(String));
            this.updateCounts();
            window.dispatchEvent(new Event('lmf-store-ready'));
            return payload;
          }})
          .catch(() => {{
            this.authenticated = false;
            this.wishlistIds = new Set();
            this.updateCounts();
            return {{ authenticated: false, ids: [] }};
          }});
      }},
      wishlist() {{
        return [...this.wishlistIds];
      }},
      toggleWishlist(id) {{
        if (!this.authenticated) {{
          window.location.href = '/compte';
          return Promise.resolve();
        }}
        const body = new URLSearchParams({{ id: String(id) }});
        return fetch('/api/wishlist/toggle', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/x-www-form-urlencoded' }},
          body
        }})
          .then(response => {{
            if (response.status === 401) {{
              window.location.href = '/compte';
              return null;
            }}
            return response.ok ? response.json() : null;
          }})
          .then(payload => {{
            if (!payload) return;
            this.authenticated = Boolean(payload.authenticated);
            this.wishlistIds = new Set((payload.ids || []).map(String));
            this.updateCounts();
            window.dispatchEvent(new Event('lmf-store-change'));
          }});
      }},
      updateCounts() {{
        const wishlistTotal = this.wishlistIds.size;
        document.querySelectorAll('[data-wishlist-count]').forEach(element => {{
          element.textContent = wishlistTotal ? `(${{wishlistTotal}})` : '';
        }});
        document.querySelectorAll('[data-wishlist-toggle]').forEach(button => {{
          const active = this.wishlistIds.has(String(button.dataset.wishlistToggle));
          button.classList.toggle('!bg-brand-gold', this.authenticated && active);
          button.classList.toggle('!border-brand-gold', this.authenticated && active);
          button.classList.toggle('!text-[#2C3328]', this.authenticated && active);
          button.classList.toggle('opacity-70', !this.authenticated);
          const label = button.querySelector('[data-wishlist-label]');
          if (label) label.textContent = this.authenticated ? (active ? 'Aimé' : 'Favori') : 'Connexion';
        }});
      }}
    }};
    window.LMF_STORE = LMF_STORE;
    document.addEventListener('click', event => {{
      const wishlistButton = event.target.closest('[data-wishlist-toggle]');
      if (wishlistButton) {{
        event.preventDefault();
        LMF_STORE.toggleWishlist(wishlistButton.dataset.wishlistToggle);
        wishlistButton.blur();
      }}
    }});
    window.addEventListener('lmf-store-change', () => LMF_STORE.updateCounts());
    LMF_STORE.loadWishlist();
  </script>
</body>
</html>"""


def message_box(text, kind="error"):
    if not text:
        return ""
    color = "border-red-200 bg-red-50 text-red-900" if kind == "error" else "border-brand-green/20 bg-brand-green/10 text-[#2C3328]"
    return f'<div class="mb-8 border {color} px-5 py-4 text-sm font-light">{esc(text)}</div>'


def checkbox_group(name, options, selected=None, color_swatches=False):
    selected = set(selected or [])
    items = []
    for option in options:
        if isinstance(option, tuple):
            label, color = option
        else:
            label, color = option, ""
        checked = "checked" if label in selected else ""
        swatch = f'<span class="w-5 h-5 border border-[#2C3328]/20 shrink-0" style="background:{esc(color)}"></span>' if color_swatches else ""
        items.append(
            f"""
            <label class="flex items-center gap-3 border border-brand-green/10 bg-white/70 px-4 py-3 text-sm text-[#2C3328] cursor-pointer hover:border-brand-green/40 transition-colors">
              <input class="accent-[#5C6656]" type="checkbox" name="{esc(name)}" value="{esc(label)}" {checked} autocomplete="off">
              {swatch}<span>{esc(label)}</span>
            </label>
            """
        )
    return "".join(items)


def account_page(message="", kind="error"):
    body = f"""
    <main class="min-h-screen pt-40 pb-20 px-4 sm:px-8">
      <section class="max-w-[1180px] mx-auto grid lg:grid-cols-[.9fr_1.1fr] gap-12 items-start">
        <div class="pt-8">
          <p class="text-[10px] uppercase tracking-[0.3em] text-brand-gold font-semibold mb-6">Espace client</p>
          <h1 class="font-serif text-5xl md:text-7xl font-light leading-tight mb-8">Créer un compte ou se connecter</h1>
          <p class="text-[#5C6656] text-lg font-light leading-relaxed max-w-md">
            Créez un compte pour enregistrer vos créations favorites dans une wishlist personnelle.
          </p>
        </div>
        <div class="grid md:grid-cols-2 gap-6">
          <form method="POST" action="/register" class="bg-white/80 border border-brand-green/10 p-7 shadow-sm">
            <h2 class="font-serif text-3xl mb-2">Nouveau compte</h2>
            <p class="text-sm text-[#5C6656] font-light mb-8">Le mot de passe est haché en base avec un sel unique.</p>
            {message_box(message, "error") if kind == "register" else ""}
            <label class="block text-[10px] uppercase tracking-[0.2em] text-brand-green font-semibold mb-2">Email</label>
            <input class="field mb-5" type="email" name="email" required autocomplete="email">
            <label class="block text-[10px] uppercase tracking-[0.2em] text-brand-green font-semibold mb-2">Mot de passe</label>
            <input class="field mb-7" type="password" name="password" required minlength="8" autocomplete="new-password">
            <button class="btn-dark w-full" type="submit"><i data-lucide="user-plus" class="w-4 h-4"></i>Créer</button>
          </form>
          <form method="POST" action="/login" class="bg-[#2C3328] text-white border border-[#3F4A38] p-7 shadow-xl">
            <h2 class="font-serif text-3xl mb-2">Connexion</h2>
            <p class="text-sm text-[#A2ADA0] font-light mb-8">Retrouvez votre wishlist et vos informations de compte.</p>
            {message_box(message, "error") if kind == "login" else ""}
            <label class="block text-[10px] uppercase tracking-[0.2em] text-brand-gold font-semibold mb-2">Email</label>
            <input class="field mb-5 !bg-white/95 !text-[#2C3328]" type="email" name="email" required autocomplete="email">
            <label class="block text-[10px] uppercase tracking-[0.2em] text-brand-gold font-semibold mb-2">Mot de passe</label>
            <input class="field mb-7 !bg-white/95 !text-[#2C3328]" type="password" name="password" required autocomplete="current-password">
            <button class="btn-dark w-full !border-brand-gold !bg-brand-gold !text-[#2C3328]" type="submit"><i data-lucide="log-in" class="w-4 h-4"></i>Entrer</button>
            <a href="/admin" class="mt-6 block text-center text-[10px] uppercase tracking-[0.2em] text-[#A2ADA0] hover:text-brand-gold transition-colors">Je suis l'admin de ce site</a>
          </form>
        </div>
      </section>
    </main>
    """
    return page("Compte", body, "Compte")


def profile_page(user, message="", kind="success"):
    is_admin = bool(user["is_admin"])
    email_field = (
        '<input class="field bg-[#EAECE8] text-[#5C6656]" value="admin" disabled autocomplete="off">'
        if is_admin
        else f'<input class="field" type="email" name="email" required value="{esc(user["email"])}" autocomplete="off">'
    )
    body = f"""
    <main class="min-h-screen pt-40 pb-20 px-4 sm:px-8">
      <section class="max-w-[1180px] mx-auto">
        <div class="flex flex-col lg:flex-row lg:items-end justify-between gap-8 border-b border-brand-green/15 pb-10 mb-10">
          <div>
            <p class="text-[10px] uppercase tracking-[0.3em] text-brand-gold font-semibold mb-5">Compte</p>
            <h1 class="font-serif text-5xl md:text-7xl font-light leading-tight">Mes informations</h1>
          </div>
          <form method="POST" action="/logout">
            <button class="btn-light" type="submit"><i data-lucide="log-out" class="w-4 h-4"></i>Déconnexion</button>
          </form>
        </div>
        {message_box(message, kind) if message else ""}
        <div class="grid lg:grid-cols-2 gap-8">
          <form method="POST" action="/profile" autocomplete="off" class="bg-white/80 border border-brand-green/10 p-7 shadow-sm">
            <h2 class="font-serif text-3xl mb-8">Profil</h2>
            <div class="grid md:grid-cols-2 gap-5 mb-5">
              <div>
                <label class="block text-[10px] uppercase tracking-[0.2em] text-brand-green font-semibold mb-2">Prénom</label>
                <input class="field" name="first_name" value="{esc(user["first_name"])}" autocomplete="off">
              </div>
              <div>
                <label class="block text-[10px] uppercase tracking-[0.2em] text-brand-green font-semibold mb-2">Nom</label>
                <input class="field" name="last_name" value="{esc(user["last_name"])}" autocomplete="off">
              </div>
            </div>
            <label class="block text-[10px] uppercase tracking-[0.2em] text-brand-green font-semibold mb-2">Adresse mail</label>
            <div class="mb-5">{email_field}</div>
            <label class="block text-[10px] uppercase tracking-[0.2em] text-brand-green font-semibold mb-2">Adresse</label>
            <textarea class="field mb-7 min-h-28" name="address" autocomplete="off">{esc(user["address"])}</textarea>
            <button class="btn-dark" type="submit"><i data-lucide="save" class="w-4 h-4"></i>Enregistrer</button>
          </form>
          <form method="POST" action="/password" autocomplete="off" class="bg-[#2C3328] text-white border border-[#3F4A38] p-7 shadow-xl">
            <h2 class="font-serif text-3xl mb-8">Mot de passe</h2>
            <label class="block text-[10px] uppercase tracking-[0.2em] text-brand-gold font-semibold mb-2">Mot de passe actuel</label>
            <input class="field mb-5 !bg-white/95 !text-[#2C3328]" type="password" name="current_password" required autocomplete="off">
            <label class="block text-[10px] uppercase tracking-[0.2em] text-brand-gold font-semibold mb-2">Nouveau mot de passe</label>
            <input class="field mb-5 !bg-white/95 !text-[#2C3328]" type="password" name="new_password" required minlength="8" autocomplete="off">
            <label class="block text-[10px] uppercase tracking-[0.2em] text-brand-gold font-semibold mb-2">Confirmer</label>
            <input class="field mb-7 !bg-white/95 !text-[#2C3328]" type="password" name="confirm_password" required minlength="8" autocomplete="off">
            <button class="btn-dark !border-brand-gold !bg-brand-gold !text-[#2C3328]" type="submit"><i data-lucide="key-round" class="w-4 h-4"></i>Modifier</button>
          </form>
        </div>
      </section>
    </main>
    """
    return page("Compte", body, "Compte")


def client_page(user=None):
    admin_controls = ""
    if user and user["is_admin"]:
        admin_controls = '<a href="/admin" class="btn-dark"><i data-lucide="settings" class="w-4 h-4"></i>Modifier les produits</a>'
    if user:
        greeting_name = (user["first_name"] or "").strip() or "cher client"
        title = f'Bonjour, <span class="italic text-brand-green">{esc(greeting_name)}</span>'
        account_controls = f"""
            <a href="/wishlist" class="btn-light"><i data-lucide="heart" class="w-4 h-4"></i>Favoris <span data-wishlist-count></span></a>
            {admin_controls}
            <form method="POST" action="/logout">
              <button class="btn-light" type="submit"><i data-lucide="log-out" class="w-4 h-4"></i>Déconnexion</button>
            </form>
        """
    else:
        title = 'Marketplace <span class="italic text-brand-green">florale</span>'
        account_controls = '<a href="/compte" class="btn-light"><i data-lucide="user" class="w-4 h-4"></i>Connexion</a>'
    body = f"""
    <main class="min-h-screen pt-40 pb-20">
      <section class="px-4 sm:px-8 lg:px-12 pb-10">
        <div class="max-w-[1500px] mx-auto flex flex-col lg:flex-row lg:items-end justify-between gap-8 border-b border-brand-green/15 pb-10">
          <div>
            <p class="text-[10px] uppercase tracking-[0.3em] text-brand-gold font-semibold mb-5">Marketplace</p>
            <h1 class="font-serif text-5xl md:text-7xl font-light leading-tight">{title}</h1>
          </div>
          <div class="flex flex-wrap gap-3">
            {account_controls}
          </div>
        </div>
      </section>
      <section class="max-w-[1500px] mx-auto px-4 sm:px-8 lg:px-12 grid lg:grid-cols-[280px_1fr] gap-10">
        <aside class="bg-white/80 border border-brand-green/10 p-6 h-max sticky top-28">
          <label class="block text-[10px] uppercase tracking-[0.2em] text-brand-green font-semibold mb-3">Recherche</label>
          <div class="relative mb-8">
            <i data-lucide="search" class="w-4 h-4 absolute left-4 top-1/2 -translate-y-1/2 text-brand-green"></i>
            <input id="search" class="field pl-11" type="search" placeholder="Bouquet, plante..." autocomplete="off">
          </div>
          <div class="mb-8">
            <p class="text-[10px] uppercase tracking-[0.2em] text-brand-green font-semibold mb-4">Types</p>
            <div id="typeFilters" class="space-y-3"></div>
          </div>
          <div class="mb-8">
            <p class="text-[10px] uppercase tracking-[0.2em] text-brand-green font-semibold mb-4">Occasions</p>
            <div id="occasionFilters" class="space-y-3"></div>
          </div>
          <div class="mb-8">
            <p class="text-[10px] uppercase tracking-[0.2em] text-brand-green font-semibold mb-4">Couleurs</p>
            <div id="colorFilters" class="flex flex-wrap gap-2"></div>
          </div>
        </aside>
        <div>
          <div class="flex items-center justify-between gap-4 mb-8">
            <p id="resultCount" class="text-[10px] uppercase tracking-[0.24em] text-[#5C6656] font-semibold"></p>
            <button id="resetFilters" class="btn-light" type="button"><i data-lucide="rotate-ccw" class="w-4 h-4"></i>Réinitialiser</button>
          </div>
          <div id="products" class="grid sm:grid-cols-2 xl:grid-cols-3 gap-7 lg:gap-10"></div>
        </div>
      </section>
    </main>
    <script>
      const state = {{ products: [] }};
      const productTypes = {json.dumps(PRODUCT_TYPES, ensure_ascii=False)};
      const occasions = {json.dumps(OCCASIONS, ensure_ascii=False)};
      const knownColors = {json.dumps(KNOWN_COLORS, ensure_ascii=False)};
      const colorMap = Object.fromEntries(knownColors.map(([name, hex]) => [name.toLowerCase(), hex]));
      const norm = value => String(value || '').trim().toLowerCase();
      const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, char => ({{
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;'
      }}[char]));
      const colorHex = name => colorMap[norm(name)] || '#C5A880';

      function productImage(product) {{
        return product.photos && product.photos.length
          ? product.photos[0]
          : '';
      }}

      function buildFilters() {{
        typeFilters.innerHTML = productTypes.map(type => `
          <label class="flex items-center gap-3 text-sm text-[#2C3328]">
            <input class="type accent-[#5C6656]" type="checkbox" value="${{escapeHtml(type)}}" autocomplete="off">
            <span>${{escapeHtml(type)}}</span>
          </label>
        `).join('');
        occasionFilters.innerHTML = occasions.map(occasion => `
          <label class="flex items-center gap-3 text-sm text-[#2C3328]">
            <input class="occasion accent-[#5C6656]" type="checkbox" value="${{escapeHtml(occasion)}}" autocomplete="off">
            <span>${{escapeHtml(occasion)}}</span>
          </label>
        `).join('');
        colorFilters.innerHTML = knownColors.map(([color, hex]) => `
          <label title="${{escapeHtml(color)}}" class="cursor-pointer">
            <input class="color sr-only peer" type="checkbox" value="${{escapeHtml(color)}}" autocomplete="off">
            <span class="block w-9 h-9 border border-[#2C3328]/20 peer-checked:ring-2 peer-checked:ring-[#2C3328] peer-checked:ring-offset-2" style="background:${{hex}}"></span>
          </label>
        `).join('');
        document.querySelectorAll('input').forEach(input => input.addEventListener('input', render));
      }}

      function selected(selector) {{
        return [...document.querySelectorAll(selector + ':checked')].map(input => input.value);
      }}

      function render() {{
        const query = norm(search.value);
        const types = selected('.type');
        const selectedOccasions = selected('.occasion');
        const colors = selected('.color').map(norm);
        const filtered = state.products.filter(product => {{
          const text = norm([product.name, product.description, product.category, ...(product.types || []), ...(product.occasions || [])].join(' '));
          const productColors = (product.colors || []).map(norm);
          const productTypes = product.types || [];
          const productOccasions = product.occasions || [];
          return (!query || text.includes(query))
            && (!types.length || types.some(type => productTypes.includes(type)))
            && (!selectedOccasions.length || selectedOccasions.some(occasion => productOccasions.includes(occasion)))
            && (!colors.length || colors.some(color => productColors.includes(color)));
        }});
        resultCount.textContent = `${{filtered.length}} création${{filtered.length > 1 ? 's' : ''}}`;
        products.innerHTML = filtered.length ? filtered.map(product => `
          <article class="group bg-white/80 border border-brand-green/10 p-5 hover:shadow-2xl hover:border-brand-green/30 transition-all">
            <a href="${{escapeHtml(product.url)}}" class="block aspect-[3/4] bg-[#EAECE8] mb-6 overflow-hidden relative">
              ${{productImage(product)
                ? `<img src="${{escapeHtml(productImage(product))}}" alt="${{escapeHtml(product.name)}}" class="w-full h-full object-cover group-hover:scale-105 transition-transform duration-700">`
                : `<div class="absolute inset-0 flex flex-col items-center justify-center text-[#8C9686]"><i data-lucide="flower-2" class="w-10 h-10 mb-3 opacity-40"></i><span class="text-[9px] tracking-[0.3em] uppercase">Sans photo</span></div>`}}
            </a>
            <div class="flex items-start justify-between gap-4 mb-4">
              <div>
                <p class="text-[10px] uppercase tracking-[0.24em] text-brand-green font-semibold mb-3">${{escapeHtml((product.types || []).join(' · '))}}</p>
                <a href="${{escapeHtml(product.url)}}" class="font-serif text-3xl text-[#2C3328] leading-tight hover:text-brand-gold transition-colors">${{escapeHtml(product.name)}}</a>
              </div>
            </div>
            <p class="text-sm text-[#5C6656] font-light leading-relaxed mb-5">${{escapeHtml(product.description)}}</p>
            <div class="flex flex-wrap gap-2 mb-6">${{(product.colors || []).map(color => `<span class="w-6 h-6 border border-[#2C3328]/20" title="${{escapeHtml(color)}}" style="background:${{colorHex(color)}}"></span>`).join('')}}</div>
            <div class="grid">
              <button class="btn-light !py-3" type="button" data-wishlist-toggle="${{product.id}}"><i data-lucide="heart" class="w-4 h-4"></i><span data-wishlist-label>Favori</span></button>
            </div>
          </article>
        `).join('') : `
          <div class="col-span-full border border-brand-green/10 bg-white/80 p-12 text-center">
            <i data-lucide="search-x" class="w-8 h-8 mx-auto text-brand-green mb-5"></i>
            <p class="font-serif text-3xl mb-2">Aucun produit trouvé</p>
            <p class="text-[#5C6656] font-light">Essayez une autre recherche ou retirez certains filtres.</p>
          </div>
        `;
        lucide.createIcons();
        if (window.LMF_STORE) window.LMF_STORE.updateCounts();
      }}

      resetFilters.addEventListener('click', () => {{
        search.value = '';
        document.querySelectorAll('input[type="checkbox"]').forEach(input => input.checked = false);
        render();
      }});

      fetch('/api/products')
        .then(response => response.json())
        .then(productsList => {{
          state.products = productsList;
          buildFilters();
          render();
        }});
    </script>
    """
    return page("Marketplace", body, "Marketplace")


def related_products(product, limit=3):
    related = []
    product_colors = set(product["colors"])
    product_types = set(product["types"])
    product_occasions = set(product["occasions"])
    for candidate in read_products():
        if candidate["id"] == product["id"]:
            continue
        score = 0
        score += len(product_colors.intersection(candidate["colors"])) * 3
        score += len(product_types.intersection(candidate["types"])) * 2
        score += len(product_occasions.intersection(candidate["occasions"]))
        if score:
            related.append((score, candidate))
    related.sort(key=lambda item: (-item[0], item[1]["id"]))
    return [candidate for _, candidate in related[:limit]]


def product_page(product):
    photos = product["photos"]
    main_photo = photos[0] if photos else ""
    thumbnails = "".join(
        f"""
        <button class="thumb aspect-square border border-brand-green/10 bg-[#EAECE8] overflow-hidden" type="button" data-photo="{esc(photo)}">
          <img src="{esc(photo)}" alt="{esc(product["name"])}" class="w-full h-full object-cover">
        </button>
        """
        for photo in photos
    )
    color_tags = "".join(
        f'<span class="inline-flex items-center gap-2 border border-brand-green/10 px-3 py-2 text-sm"><span class="w-4 h-4 border border-[#2C3328]/20" style="background:{esc(dict(KNOWN_COLORS).get(color, "#C5A880"))}"></span>{esc(color)}</span>'
        for color in product["colors"]
    )
    related_cards = "".join(
        f"""
        <a href="{item["url"]}" class="group block bg-white/80 border border-brand-green/10 p-4 hover:shadow-xl transition-all">
          <div class="aspect-[4/5] bg-[#EAECE8] overflow-hidden mb-4">
            <img src="{esc(item["photos"][0])}" alt="{esc(item["name"])}" class="w-full h-full object-cover group-hover:scale-105 transition-transform duration-700">
          </div>
          <p class="text-[10px] uppercase tracking-[0.2em] text-brand-green font-semibold mb-2">{esc(", ".join(item["types"]))}</p>
          <h3 class="font-serif text-2xl">{esc(item["name"])}</h3>
        </a>
        """
        for item in related_products(product)
    )
    body = f"""
    <main class="min-h-screen pt-40 pb-20 px-4 sm:px-8 lg:px-12">
      <section class="max-w-[1500px] mx-auto grid lg:grid-cols-[1.1fr_.9fr] gap-10 lg:gap-16">
        <div>
          <div id="zoomArea" class="relative aspect-[4/5] lg:aspect-[5/4] bg-[#EAECE8] overflow-hidden border border-brand-green/10 cursor-zoom-in select-none" data-zoom-src="{esc(main_photo)}">
            <img id="mainPhoto" src="{esc(main_photo)}" alt="{esc(product["name"])}" class="absolute inset-0 w-full h-full object-cover">
            <div id="zoomLayer" class="absolute inset-0 opacity-0 pointer-events-none bg-no-repeat transition-opacity duration-150" style="background-image:url('{esc(main_photo)}'); background-size:auto 220%; background-position:center center;"></div>
          </div>
          <div class="grid grid-cols-4 sm:grid-cols-6 gap-3 mt-4">{thumbnails}</div>
        </div>
        <article class="lg:pt-10">
          <a href="/client" class="inline-flex items-center gap-2 text-[10px] uppercase tracking-[0.2em] text-brand-green hover:text-brand-gold transition-colors mb-8"><i data-lucide="arrow-left" class="w-4 h-4"></i>Marketplace</a>
          <p class="font-mono text-xs text-[#5C6656] mb-4">Produit #{product["id"]}</p>
          <p class="text-[10px] uppercase tracking-[0.3em] text-brand-gold font-semibold mb-5">{esc(", ".join(product["types"]))}</p>
          <h1 class="font-serif text-5xl md:text-7xl font-light leading-tight mb-6">{esc(product["name"])}</h1>
          <div class="grid sm:grid-cols-[minmax(0,260px)] gap-3 mb-8">
            <button class="btn-light" type="button" data-wishlist-toggle="{product["id"]}"><i data-lucide="heart" class="w-4 h-4"></i><span data-wishlist-label>Favori</span></button>
          </div>
          <p class="text-[#5C6656] text-lg font-light leading-relaxed mb-8">{esc(product["description"])}</p>
          <div class="mb-7">
            <p class="text-[10px] uppercase tracking-[0.2em] text-brand-green font-semibold mb-3">Occasions</p>
            <div class="flex flex-wrap gap-2">{''.join(f'<span class="border border-brand-green/10 px-3 py-2 text-sm">{esc(item)}</span>' for item in product["occasions"])}</div>
          </div>
          <div>
            <p class="text-[10px] uppercase tracking-[0.2em] text-brand-green font-semibold mb-3">Couleurs</p>
            <div class="flex flex-wrap gap-2">{color_tags}</div>
          </div>
        </article>
      </section>
      <section class="max-w-[1500px] mx-auto mt-20">
        <div class="border-t border-brand-green/15 pt-12">
          <h2 class="font-serif text-4xl md:text-5xl font-light mb-8">Produits associés</h2>
          <div class="grid sm:grid-cols-2 lg:grid-cols-3 gap-8">{related_cards or '<p class="text-[#5C6656] font-light">Aucun produit associé pour le moment.</p>'}</div>
        </div>
      </section>
    </main>
    <script>
      let zoomPinned = false;
      const zoomArea = document.getElementById('zoomArea');
      const mainPhoto = document.getElementById('mainPhoto');
      const zoomLayer = document.getElementById('zoomLayer');

      const imageUrl = value => 'url("' + String(value || '').replace(/"/g, '%22') + '")';
      const setZoomPhoto = src => {{
        mainPhoto.src = src;
        zoomArea.dataset.zoomSrc = src;
        zoomLayer.style.backgroundImage = imageUrl(src);
      }};
      const setZoomPosition = event => {{
        const rect = zoomArea.getBoundingClientRect();
        const x = Math.max(0, Math.min(100, ((event.clientX - rect.left) / rect.width) * 100));
        const y = Math.max(0, Math.min(100, ((event.clientY - rect.top) / rect.height) * 100));
        zoomLayer.style.backgroundPosition = `${{x}}% ${{y}}%`;
      }};
      const showZoom = event => {{
        if (!zoomArea.dataset.zoomSrc) return;
        setZoomPosition(event);
        zoomLayer.classList.add('opacity-100');
      }};
      const hideZoom = () => {{
        if (!zoomPinned) {{
          zoomLayer.classList.remove('opacity-100');
        }}
      }};

      zoomArea.addEventListener('pointermove', event => {{
        if (event.pointerType === 'touch') return;
        showZoom(event);
      }});
      zoomArea.addEventListener('pointerleave', hideZoom);
      zoomArea.addEventListener('click', event => {{
        zoomPinned = !zoomPinned;
        zoomArea.classList.toggle('cursor-zoom-out', zoomPinned);
        zoomArea.classList.toggle('cursor-zoom-in', !zoomPinned);
        if (zoomPinned) {{
          showZoom(event);
        }} else {{
          zoomLayer.classList.remove('opacity-100');
        }}
      }});
      document.querySelectorAll('.thumb').forEach(button => {{
        button.addEventListener('click', () => {{
          setZoomPhoto(button.dataset.photo);
          zoomPinned = false;
          zoomLayer.classList.remove('opacity-100');
          zoomArea.classList.remove('cursor-zoom-out');
          zoomArea.classList.add('cursor-zoom-in');
        }});
      }});
    </script>
    """
    return page(product["name"], body, "Marketplace")


def wishlist_page():
    body = """
    <main class="min-h-screen pt-40 pb-20 px-4 sm:px-8 lg:px-12">
      <section class="max-w-[1500px] mx-auto">
        <div class="flex flex-col lg:flex-row lg:items-end justify-between gap-8 border-b border-brand-green/15 pb-10 mb-10">
          <div>
            <p class="text-[10px] uppercase tracking-[0.3em] text-brand-gold font-semibold mb-5">Favoris</p>
            <h1 class="font-serif text-5xl md:text-7xl font-light leading-tight">Votre wishlist</h1>
          </div>
          <a href="/client" class="btn-light"><i data-lucide="arrow-left" class="w-4 h-4"></i>Retour au marketplace</a>
        </div>
        <div id="wishlistItems" class="grid sm:grid-cols-2 xl:grid-cols-3 gap-7 lg:gap-10"></div>
      </section>
    </main>
    <script>
      const wishlistItems = document.getElementById('wishlistItems');
      const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, char => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;'
      }[char]));
      const imageFor = product => product.photos && product.photos.length ? product.photos[0] : '';

      function renderWishlist(products) {
        const liked = window.LMF_STORE.wishlist();
        const rows = products.filter(product => liked.includes(String(product.id)));
        if (!rows.length) {
          wishlistItems.innerHTML = `
            <div class="col-span-full bg-white/80 border border-brand-green/10 p-10 md:p-14 text-center">
              <i data-lucide="heart" class="w-10 h-10 mx-auto mb-5 text-brand-green opacity-60"></i>
              <h2 class="font-serif text-4xl mb-3">Aucun favori</h2>
              <p class="text-[#5C6656] font-light mb-8">Cliquez sur le coeur d'un produit pour le retrouver ici.</p>
              <a href="/client" class="btn-dark"><i data-lucide="arrow-right" class="w-4 h-4"></i>Découvrir les produits</a>
            </div>
          `;
          lucide.createIcons();
          return;
        }
        wishlistItems.innerHTML = rows.map(product => `
          <article class="group bg-white/80 border border-brand-green/10 p-5 hover:shadow-2xl hover:border-brand-green/30 transition-all">
            <a href="${escapeHtml(product.url)}" class="block aspect-[3/4] bg-[#EAECE8] mb-6 overflow-hidden relative">
              ${imageFor(product)
                ? `<img src="${escapeHtml(imageFor(product))}" alt="${escapeHtml(product.name)}" class="w-full h-full object-cover group-hover:scale-105 transition-transform duration-700">`
                : `<div class="absolute inset-0 flex items-center justify-center text-brand-green/50"><i data-lucide="flower-2" class="w-10 h-10"></i></div>`}
            </a>
            <p class="font-mono text-xs text-[#5C6656] mb-3">#${product.id}</p>
            <a href="${escapeHtml(product.url)}" class="font-serif text-3xl text-[#2C3328] leading-tight hover:text-brand-gold transition-colors">${escapeHtml(product.name)}</a>
            <p class="text-sm text-[#5C6656] font-light leading-relaxed mt-4 mb-5">${escapeHtml(product.description)}</p>
            <div class="flex items-center justify-between gap-4 mb-6">
              <p class="text-[10px] uppercase tracking-[0.2em] text-brand-green font-semibold">${escapeHtml((product.types || []).join(' · '))}</p>
            </div>
            <div class="grid">
              <button class="btn-light !py-3" type="button" data-wishlist-toggle="${product.id}"><i data-lucide="heart" class="w-4 h-4"></i><span data-wishlist-label>Aimé</span></button>
            </div>
          </article>
        `).join('');
        lucide.createIcons();
        window.LMF_STORE.updateCounts();
      }

      function withStore(callback) {
        if (window.LMF_STORE) {
          callback();
          return;
        }
        setTimeout(() => withStore(callback), 20);
      }

      withStore(() => {
        Promise.all([
          fetch('/api/products').then(response => response.json()),
          window.LMF_STORE.loadWishlist()
        ])
          .then(([products]) => {
            renderWishlist(products);
            window.addEventListener('lmf-store-change', () => renderWishlist(products));
          });
      });
    </script>
    """
    return page("Favoris", body, "Favoris")


def not_found_page():
    lat = "50.7512057"
    lon = "2.2539618"
    map_url = (
        "https://www.openstreetmap.org/export/embed.html"
        "?bbox=2.2499618%2C50.7492057%2C2.2579618%2C50.7532057"
        "&layer=mapnik"
        f"&marker={lat}%2C{lon}"
    )
    body = f"""
    <main class="min-h-screen pt-40 pb-20 px-4 sm:px-8 lg:px-12">
      <section class="max-w-[1300px] mx-auto grid lg:grid-cols-[.8fr_1.2fr] gap-10 lg:gap-14 items-center">
        <div>
          <p class="text-[10px] uppercase tracking-[0.3em] text-brand-gold font-semibold mb-6">Erreur 404</p>
          <h1 class="font-serif text-5xl md:text-7xl font-light leading-tight mb-8">
            Page introuvable,<br>
            <span class="italic text-brand-green">mais ma boutique, elle, est trouvable</span>
          </h1>
          <p class="text-[#5C6656] text-lg font-light leading-relaxed mb-10 max-w-xl">
            Retrouvez Le Marais Fleuri au 40 rue de Dunkerque, 62500 Saint-Omer.
          </p>
          <div class="flex flex-wrap gap-3">
            <a href="/" class="btn-dark"><i data-lucide="home" class="w-4 h-4"></i>Retour au menu</a>
            <a href="/client" class="btn-light"><i data-lucide="store" class="w-4 h-4"></i>Marketplace</a>
          </div>
        </div>
        <div class="bg-white/80 border border-brand-green/10 p-4 sm:p-5 shadow-sm">
          <div class="aspect-[4/3] min-h-[360px] bg-[#EAECE8] overflow-hidden border border-brand-green/10">
            <iframe
              title="Carte OpenStreetMap - Le Marais Fleuri"
              class="w-full h-full"
              src="{map_url}"
              loading="lazy"
              referrerpolicy="no-referrer-when-downgrade">
            </iframe>
          </div>
          <div class="mt-5 flex items-start gap-3 text-[#5C6656]">
            <i data-lucide="map-pin" class="w-5 h-5 text-brand-gold shrink-0 mt-1"></i>
            <p class="font-light leading-relaxed">
              <span class="block text-[#2C3328] font-medium">Le Marais Fleuri</span>
              40 rue de Dunkerque, 62500 Saint-Omer
            </p>
          </div>
        </div>
      </section>
    </main>
    """
    return page("Page introuvable", body)


def admin_login_page(message=""):
    body = f"""
    <main class="min-h-screen pt-40 pb-20 px-4 sm:px-8">
      <section class="max-w-[820px] mx-auto">
        <p class="text-[10px] uppercase tracking-[0.3em] text-brand-gold font-semibold mb-6">Administration</p>
        <h1 class="font-serif text-5xl md:text-7xl font-light leading-tight mb-8">Entrer la passphrase</h1>
        <p class="text-[#5C6656] text-lg font-light leading-relaxed mb-10 max-w-xl">
          La passphrase fait exactement 22 caractères. Elle est générée à chaque démarrage et affichée dans le terminal du serveur.
        </p>
        <form method="POST" action="/admin/login" class="bg-white/80 border border-brand-green/10 p-8 shadow-sm">
          {message_box(message, "error")}
          <label class="block text-[10px] uppercase tracking-[0.2em] text-brand-green font-semibold mb-2">Passphrase admin</label>
          <input class="field mb-7 font-mono tracking-[0.18em]" type="password" name="passphrase" required minlength="22" maxlength="22" autocomplete="off">
          <button class="btn-dark" type="submit"><i data-lucide="shield-check" class="w-4 h-4"></i>Ouvrir l'admin</button>
        </form>
      </section>
    </main>
    """
    return page("Admin", body, "Admin")


def admin_analytics_page():
    summary, product_rows, recent_rows = analytics_snapshot()
    metric_cards = [
        ("Clients en live", summary["live_clients"], f"Actifs sur les {LIVE_WINDOW_MINUTES} dernières minutes"),
        ("Clients depuis la création", summary["total_clients"], "Visiteurs uniques non-bots"),
        ("Comptes client", summary["registered_clients"], "Comptes inscrits hors admin"),
        ("Ajouts wishlist", summary["wishlist_total"], "Total des créations enregistrées"),
        ("Pages vues", summary["pageviews"], "Pages publiques non-bots"),
    ]
    cards = "".join(
        f"""
        <article class="bg-white/80 border border-brand-green/10 p-6 shadow-sm">
          <p class="text-[10px] uppercase tracking-[0.2em] text-brand-green font-semibold mb-4">{esc(label)}</p>
          <p class="font-serif text-5xl text-[#2C3328] mb-3">{value}</p>
          <p class="text-sm text-[#5C6656] font-light">{esc(help_text)}</p>
        </article>
        """
        for label, value, help_text in metric_cards
    )
    product_stats = "".join(
        f"""
        <tr class="border-b border-brand-green/10 align-top">
          <td class="py-4 pr-4 font-mono text-xs text-[#5C6656]">#{row["id"]}</td>
          <td class="py-4 pr-4">
            <a href="/produit/{row["id"]}" class="font-serif text-2xl hover:text-brand-gold transition-colors">{esc(row["name"])}</a>
            <p class="text-xs text-[#5C6656] mt-1">{esc(", ".join(canonical_values(json_list(row["product_types"]), PRODUCT_TYPES)) or "Sans type")} · {esc(", ".join(canonical_values(json_list(row["occasions"]), OCCASIONS)) or "Toutes occasions")}</p>
          </td>
          <td class="py-4 text-right">
            <span class="inline-flex items-center justify-center min-w-14 px-4 py-2 bg-brand-green/10 text-brand-green font-serif text-2xl">{row["wishlist_count"]}</span>
          </td>
        </tr>
        """
        for row in product_rows
    )
    recent_pages = "".join(
        f"""
        <tr class="border-b border-brand-green/10">
          <td class="py-3 pr-4 font-mono text-xs text-[#5C6656]">{esc(row["path"])}</td>
          <td class="py-3 text-right font-serif text-2xl">{row["views"]}</td>
        </tr>
        """
        for row in recent_rows
    )
    body = f"""
    <main class="min-h-screen pt-40 pb-20 px-4 sm:px-8 lg:px-12">
      <section class="max-w-[1500px] mx-auto">
        <div class="flex flex-col lg:flex-row lg:items-end justify-between gap-8 border-b border-brand-green/15 pb-10 mb-10">
          <div>
            <p class="text-[10px] uppercase tracking-[0.3em] text-brand-gold font-semibold mb-5">Administration</p>
            <h1 class="font-serif text-5xl md:text-7xl font-light leading-tight">Analytics</h1>
          </div>
          <div class="flex flex-wrap gap-3">
            <a class="btn-light" href="/admin"><i data-lucide="arrow-left" class="w-4 h-4"></i>Retour admin</a>
            <a class="btn-dark" href="/admin/analytics"><i data-lucide="refresh-cw" class="w-4 h-4"></i>Rafraîchir</a>
          </div>
        </div>

        <div class="grid sm:grid-cols-2 xl:grid-cols-5 gap-5 mb-10">
          {cards}
        </div>

        <div class="grid xl:grid-cols-[1fr_420px] gap-10">
          <section class="bg-white/80 border border-brand-green/10 p-6 sm:p-8 shadow-sm">
            <div class="flex items-center justify-between gap-4 mb-6">
              <h2 class="font-serif text-3xl">Wishlists par produit</h2>
              <span class="text-[10px] uppercase tracking-[0.2em] text-brand-green font-semibold">{len(product_rows)} produits</span>
            </div>
            <div class="overflow-x-auto">
              <table class="w-full text-left">
                <thead>
                  <tr class="border-b border-brand-green/15 text-[10px] uppercase tracking-[0.18em] text-brand-green">
                    <th class="pb-3 pr-4 font-semibold">ID</th>
                    <th class="pb-3 pr-4 font-semibold">Produit</th>
                    <th class="pb-3 text-right font-semibold">Wishlist</th>
                  </tr>
                </thead>
                <tbody>{product_stats or '<tr><td class="py-8 text-[#5C6656] font-light" colspan="3">Aucun produit publié.</td></tr>'}</tbody>
              </table>
            </div>
          </section>

          <section class="bg-white/80 border border-brand-green/10 p-6 sm:p-8 shadow-sm h-max">
            <h2 class="font-serif text-3xl mb-6">Pages vues sur 7 jours</h2>
            <div class="overflow-x-auto">
              <table class="w-full text-left">
                <tbody>{recent_pages or '<tr><td class="py-8 text-[#5C6656] font-light" colspan="2">Aucune visite publique enregistrée.</td></tr>'}</tbody>
              </table>
            </div>
          </section>
        </div>
      </section>
    </main>
    """
    return page("Analytics", body, "Admin")


def admin_page(message="", kind="success", edit_product=None):
    products = read_products()
    is_edit = edit_product is not None
    product = edit_product or {
        "id": "",
        "name": "",
        "description": "",
        "types": [],
        "occasions": [],
        "colors": [],
        "photos": [],
    }
    action = "/admin/product/update" if is_edit else "/admin/product"
    submit_label = "Modifier le produit" if is_edit else "Publier le produit"
    title = "Modifier un produit" if is_edit else "Publier un produit"
    photo_required = "" if is_edit else "required"
    existing_photos = "".join(
        f"""
        <label class="relative block aspect-square bg-[#EAECE8] overflow-hidden border border-brand-green/10">
          <img src="{esc(photo)}" alt="Photo produit" class="w-full h-full object-cover">
          <span class="absolute left-2 bottom-2 bg-white/90 px-2 py-1 text-[10px] uppercase tracking-[0.12em] text-[#2C3328]">
            <input type="checkbox" name="remove_photos" value="{esc(photo)}" autocomplete="off"> supprimer
          </span>
        </label>
        """
        for photo in product["photos"]
    )
    rows = "".join(
        f"""
        <tr class="border-b border-brand-green/10 align-top">
          <td class="py-4 pr-4 font-mono text-xs text-[#5C6656]">#{product["id"]}</td>
          <td class="py-4 pr-4">
            <p class="font-serif text-2xl">{esc(product["name"])}</p>
            <p class="text-xs text-[#5C6656] mt-1">{esc(", ".join(product["types"]))} · {esc(", ".join(product["occasions"]) or "Toutes occasions")} · {esc(", ".join(product["colors"]))}</p>
          </td>
          <td class="py-4">
            <div class="flex flex-wrap gap-2 justify-end">
              <a class="btn-light !py-2" href="{product["url"]}"><i data-lucide="eye" class="w-4 h-4"></i>Voir</a>
              <a class="btn-light !py-2" href="/admin?edit={product["id"]}"><i data-lucide="pencil" class="w-4 h-4"></i>Modifier</a>
              <form method="POST" action="/admin/delete" onsubmit="return confirm('Supprimer ce produit ?')">
              <input type="hidden" name="id" value="{product["id"]}">
              <button class="btn-light !py-2" type="submit"><i data-lucide="trash-2" class="w-4 h-4"></i>Supprimer</button>
              </form>
            </div>
          </td>
        </tr>
        """
        for product in products
    )
    body = f"""
    <main class="min-h-screen pt-40 pb-20 px-4 sm:px-8 lg:px-12">
      <section class="max-w-[1500px] mx-auto">
        <div class="flex flex-col lg:flex-row lg:items-end justify-between gap-8 border-b border-brand-green/15 pb-10 mb-10">
          <div>
            <p class="text-[10px] uppercase tracking-[0.3em] text-brand-gold font-semibold mb-5">Administration</p>
            <h1 class="font-serif text-5xl md:text-7xl font-light leading-tight">{title}</h1>
          </div>
          <div class="flex flex-wrap gap-3">
            <a class="btn-dark" href="/admin/analytics"><i data-lucide="chart-no-axes-column" class="w-4 h-4"></i>Analytics</a>
            <form method="POST" action="/admin/logout">
              <button class="btn-light" type="submit"><i data-lucide="log-out" class="w-4 h-4"></i>Fermer l'admin</button>
            </form>
          </div>
        </div>
        {message_box(message, kind) if message else ""}
        <div class="grid lg:grid-cols-[1fr_.9fr] gap-10">
          <form method="POST" action="{action}" enctype="multipart/form-data" autocomplete="off" class="bg-white/80 border border-brand-green/10 p-6 sm:p-8 shadow-sm">
            {'<input type="hidden" name="id" value="' + esc(product["id"]) + '">' if is_edit else ''}
            <div class="mb-5">
              <div>
                <label class="block text-[10px] uppercase tracking-[0.2em] text-brand-green font-semibold mb-2">Nom</label>
                <input class="field" name="name" required placeholder="Bouquet romantique" value="{esc(product["name"])}" autocomplete="off">
              </div>
            </div>
            <div class="grid md:grid-cols-2 gap-7 mb-7">
              <div>
                <p class="text-[10px] uppercase tracking-[0.2em] text-brand-green font-semibold mb-3">Types</p>
                <div class="grid gap-3">{checkbox_group("product_types", PRODUCT_TYPES, product["types"])}</div>
              </div>
              <div>
                <p class="text-[10px] uppercase tracking-[0.2em] text-brand-green font-semibold mb-3">Occasions</p>
                <div class="grid gap-3">{checkbox_group("occasions", OCCASIONS, product["occasions"])}</div>
              </div>
            </div>
            <div class="mb-7">
              <p class="text-[10px] uppercase tracking-[0.2em] text-brand-green font-semibold mb-3">Couleurs</p>
              <div class="grid sm:grid-cols-2 md:grid-cols-3 gap-3">{checkbox_group("colors", KNOWN_COLORS, product["colors"], True)}</div>
            </div>
            <label class="block text-[10px] uppercase tracking-[0.2em] text-brand-green font-semibold mb-2">Description</label>
            <textarea class="field mb-5 min-h-32" name="description" required placeholder="Décrivez simplement la création, sa taille, son usage..." autocomplete="off">{esc(product["description"])}</textarea>
            {('<div class="mb-5"><p class="text-[10px] uppercase tracking-[0.2em] text-brand-green font-semibold mb-3">Photos actuelles</p><div class="grid grid-cols-2 sm:grid-cols-4 gap-3">' + existing_photos + '</div></div>') if existing_photos else ''}
            <label class="block text-[10px] uppercase tracking-[0.2em] text-brand-green font-semibold mb-2">Photos</label>
            <input class="field mb-7" name="photos" type="file" accept="image/*" multiple {photo_required} autocomplete="off">
            <div class="flex flex-wrap gap-3">
              <button class="btn-dark" type="submit"><i data-lucide="plus" class="w-4 h-4"></i>{submit_label}</button>
              {'<a class="btn-light" href="/admin">Annuler</a>' if is_edit else ''}
            </div>
          </form>
          <div class="bg-white/80 border border-brand-green/10 p-6 sm:p-8 shadow-sm">
            <div class="flex items-center justify-between gap-4 mb-6">
              <h2 class="font-serif text-3xl">Produits publiés</h2>
              <span class="text-[10px] uppercase tracking-[0.2em] text-brand-green font-semibold">{len(products)} total</span>
            </div>
            <div class="overflow-x-auto">
              <table class="w-full text-left">
                <tbody>{rows or '<tr><td class="py-8 text-[#5C6656] font-light">Aucun produit publié pour le moment.</td></tr>'}</tbody>
              </table>
            </div>
          </div>
        </div>
      </section>
    </main>
    """
    return page("Admin", body, "Admin")


class App(BaseHTTPRequestHandler):
    server_version = "LeMaraisFleuri/1.0"

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")

    def parsed_cookies(self):
        jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        return {key: morsel.value for key, morsel in jar.items()}

    def send_html(self, content, status=200):
        body = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location, set_cookie=None):
        self.send_response(303)
        self.send_header("Location", location)
        if set_cookie:
            if isinstance(set_cookie, (list, tuple)):
                for cookie_value in set_cookie:
                    self.send_header("Set-Cookie", cookie_value)
            else:
                self.send_header("Set-Cookie", set_cookie)
        self.end_headers()

    def read_form(self):
        size = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(size).decode("utf-8")
        return {key: values[0] for key, values in parse_qs(data).items()}

    def current_user(self):
        token = self.parsed_cookies().get("lmf_session")
        if not token:
            return None
        with db() as conn:
            return conn.execute(
                """
                SELECT users.* FROM sessions
                JOIN users ON users.id = sessions.user_id
                WHERE sessions.token = ?
                """,
                (token,),
            ).fetchone()

    def is_admin(self):
        return self.parsed_cookies().get("lmf_admin") in ADMIN_SESSIONS

    def client_ip(self):
        forwarded = self.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",", 1)[0].strip()
        real_ip = self.headers.get("X-Real-IP", "").strip()
        return real_ip or self.client_address[0]

    def track_public_visit(self, path):
        user_agent = self.headers.get("User-Agent", "")
        if is_bot_user_agent(user_agent):
            return
        user = self.current_user()
        if user and user["is_admin"]:
            return
        normalized_path = "/" if path == "/home.html" else path
        visitor_key = hashlib.sha256(f"{self.client_ip()}|{user_agent}".encode("utf-8")).hexdigest()
        with db() as conn:
            conn.execute(
                """
                INSERT INTO site_visitors (visitor_key, user_agent, last_path)
                VALUES (?, ?, ?)
                ON CONFLICT(visitor_key) DO UPDATE SET
                  last_seen = CURRENT_TIMESTAMP,
                  user_agent = excluded.user_agent,
                  last_path = excluded.last_path
                """,
                (visitor_key, user_agent[:500], normalized_path[:500]),
            )
            conn.execute(
                "INSERT INTO site_pageviews (visitor_key, path) VALUES (?, ?)",
                (visitor_key, normalized_path[:500]),
            )

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/home.html"):
            self.track_public_visit(path)
            self.serve_file(ROOT / "home.html", "text/html; charset=utf-8")
        elif Path(path).name in FAVICON_FILES and "/" + Path(path).name == path:
            filename = Path(path).name
            content_type = "application/manifest+json" if filename.endswith(".webmanifest") else None
            self.serve_file(LOGO_DIR / filename, content_type, cache_seconds=86400)
        elif path.startswith("/logo/"):
            filename = Path(unquote(path.removeprefix("/logo/"))).name
            content_type = "application/manifest+json" if filename.endswith(".webmanifest") else None
            self.serve_file(LOGO_DIR / filename, content_type, cache_seconds=86400)
        elif path == "/compte":
            self.track_public_visit(path)
            user = self.current_user()
            if user:
                self.send_html(profile_page(user))
            else:
                self.send_html(account_page())
        elif path == "/client":
            self.track_public_visit(path)
            user = self.current_user()
            self.send_html(client_page(user))
        elif path == "/wishlist":
            if not self.current_user():
                self.redirect("/compte")
                return
            self.track_public_visit(path)
            self.send_html(wishlist_page())
        elif path == "/admin":
            if not self.is_admin():
                self.send_html(admin_login_page())
                return
            edit_id = parse_qs(parsed.query).get("edit", [""])[0]
            self.send_html(admin_page(edit_product=read_product(edit_id)) if edit_id else admin_page())
        elif path == "/admin/analytics":
            if not self.is_admin():
                self.send_html(admin_login_page())
                return
            self.send_html(admin_analytics_page())
        elif path.startswith("/produit/"):
            product_id = path.removeprefix("/produit/")
            product = read_product(product_id)
            if not product:
                self.send_html(not_found_page(), 404)
                return
            self.track_public_visit(path)
            self.send_html(product_page(product))
        elif path == "/api/products":
            json_response(self, public_products())
        elif path == "/api/wishlist":
            user = self.current_user()
            if not user:
                json_response(self, {"authenticated": False, "ids": []})
                return
            json_response(self, {"authenticated": True, "ids": read_wishlist_ids(user["id"])})
        elif path.startswith("/uploads/"):
            self.serve_upload(path)
        else:
            self.send_html(not_found_page(), 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/register":
            self.register()
        elif path == "/login":
            self.login()
        elif path == "/profile":
            self.update_profile()
        elif path == "/password":
            self.update_password()
        elif path == "/logout":
            self.logout()
        elif path == "/api/wishlist/toggle":
            self.toggle_wishlist()
        elif path == "/admin/login":
            self.admin_login()
        elif path == "/admin/logout":
            self.admin_logout()
        elif path == "/admin/product":
            self.create_product()
        elif path == "/admin/product/update":
            self.update_product()
        elif path == "/admin/delete":
            self.delete_product()
        else:
            self.send_error(404)

    def serve_file(self, path, content_type=None, cache_seconds=0):
        if not path.exists() or not path.is_file():
            self.send_error(404)
            return
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", content_type or mimetypes.guess_type(str(path))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(size))
        if cache_seconds:
            self.send_header("Cache-Control", f"public, max-age={cache_seconds}, immutable")
        else:
            self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            with path.open("rb") as file:
                shutil.copyfileobj(file, self.wfile, length=1024 * 256)
        except (BrokenPipeError, ConnectionResetError):
            return

    def serve_upload(self, path):
        filename = Path(unquote(path.removeprefix("/uploads/"))).name
        target = UPLOAD_DIR / filename
        self.serve_file(target, cache_seconds=86400)

    def create_session(self, user_id):
        token = secrets.token_urlsafe(SESSION_BYTES)
        with db() as conn:
            conn.execute("INSERT INTO sessions (token, user_id) VALUES (?, ?)", (token, user_id))
        return f"lmf_session={token}; Path=/; HttpOnly; SameSite=Lax"

    def register(self):
        form = self.read_form()
        email = form.get("email", "").strip().lower()
        password = form.get("password", "")
        if not email or email == "admin" or len(password) < 8:
            self.send_html(account_page("Email obligatoire et mot de passe de 8 caractères minimum.", "register"), 400)
            return
        password_hash, salt = hash_password(password)
        try:
            with db() as conn:
                cur = conn.execute(
                    "INSERT INTO users (email, password_hash, salt) VALUES (?, ?, ?)",
                    (email, password_hash, salt),
                )
                user_id = cur.lastrowid
        except sqlite3.IntegrityError:
            self.send_html(account_page("Un compte existe déjà avec cet email.", "register"), 409)
            return
        self.redirect("/client", self.create_session(user_id))

    def login(self):
        form = self.read_form()
        email = form.get("email", "").strip().lower()
        password = form.get("password", "")
        with db() as conn:
            user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if not user or not verify_password(password, user["password_hash"], user["salt"]):
            self.send_html(account_page("Email ou mot de passe incorrect.", "login"), 401)
            return
        self.redirect("/client", self.create_session(user["id"]))

    def update_profile(self):
        user = self.current_user()
        if not user:
            self.redirect("/compte")
            return
        form = self.read_form()
        first_name = form.get("first_name", "").strip()
        last_name = form.get("last_name", "").strip()
        address = form.get("address", "").strip()
        email = form.get("email", "").strip().lower()
        try:
            with db() as conn:
                if user["is_admin"]:
                    conn.execute(
                        "UPDATE users SET first_name = ?, last_name = ?, address = ? WHERE id = ?",
                        (first_name, last_name, address, user["id"]),
                    )
                else:
                    if not email or email == "admin":
                        self.send_html(profile_page(user, "Adresse mail invalide.", "error"), 400)
                        return
                    conn.execute(
                        "UPDATE users SET first_name = ?, last_name = ?, address = ?, email = ? WHERE id = ?",
                        (first_name, last_name, address, email, user["id"]),
                    )
        except sqlite3.IntegrityError:
            self.send_html(profile_page(user, "Cette adresse mail est déjà utilisée.", "error"), 409)
            return
        self.send_html(profile_page(self.current_user(), "Informations enregistrées.", "success"))

    def update_password(self):
        user = self.current_user()
        if not user:
            self.redirect("/compte")
            return
        form = self.read_form()
        current_password = form.get("current_password", "")
        new_password = form.get("new_password", "")
        confirm_password = form.get("confirm_password", "")
        if not verify_password(current_password, user["password_hash"], user["salt"]):
            self.send_html(profile_page(user, "Mot de passe actuel incorrect.", "error"), 401)
            return
        if len(new_password) < 8 or new_password != confirm_password:
            self.send_html(profile_page(user, "Le nouveau mot de passe doit faire 8 caractères minimum et être confirmé.", "error"), 400)
            return
        password_hash, salt = hash_password(new_password)
        with db() as conn:
            conn.execute("UPDATE users SET password_hash = ?, salt = ? WHERE id = ?", (password_hash, salt, user["id"]))
        self.send_html(profile_page(self.current_user(), "Mot de passe modifié.", "success"))

    def logout(self):
        token = self.parsed_cookies().get("lmf_session")
        if token:
            with db() as conn:
                conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        self.redirect("/compte", "lmf_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")

    def toggle_wishlist(self):
        user = self.current_user()
        if not user:
            json_response(self, {"authenticated": False, "ids": []}, 401)
            return
        form = self.read_form()
        product_id = form.get("id", "").strip()
        product = read_product(product_id)
        if not product:
            json_response(self, {"error": "Produit introuvable."}, 404)
            return
        with db() as conn:
            existing = conn.execute(
                "SELECT 1 FROM user_wishlist WHERE user_id = ? AND product_id = ?",
                (user["id"], product["id"]),
            ).fetchone()
            if existing:
                conn.execute(
                    "DELETE FROM user_wishlist WHERE user_id = ? AND product_id = ?",
                    (user["id"], product["id"]),
                )
            else:
                conn.execute(
                    "INSERT OR IGNORE INTO user_wishlist (user_id, product_id) VALUES (?, ?)",
                    (user["id"], product["id"]),
                )
        json_response(self, {"authenticated": True, "ids": read_wishlist_ids(user["id"])})

    def admin_login(self):
        form = self.read_form()
        if hmac.compare_digest(form.get("passphrase", ""), ADMIN_PASSPHRASE):
            token = secrets.token_urlsafe(SESSION_BYTES)
            ADMIN_SESSIONS.add(token)
            with db() as conn:
                admin_user = conn.execute("SELECT id FROM users WHERE email = 'admin'").fetchone()
            cookies_to_set = [f"lmf_admin={token}; Path=/; HttpOnly; SameSite=Lax"]
            if admin_user:
                cookies_to_set.append(self.create_session(admin_user["id"]))
            self.redirect("/admin", cookies_to_set)
            return
        self.send_html(admin_login_page("Passphrase incorrecte."), 401)

    def admin_logout(self):
        token = self.parsed_cookies().get("lmf_admin")
        if token:
            ADMIN_SESSIONS.discard(token)
        self.redirect("/admin", "lmf_admin=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")

    def multipart_form(self):
        size = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(size)
        content_type = self.headers.get("Content-Type", "")
        raw_message = (
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
            + body
        )
        message = BytesParser(policy=policy.default).parsebytes(raw_message)
        form = {}
        if not message.is_multipart():
            return form
        for part in message.iter_parts():
            if part.get_content_disposition() != "form-data":
                continue
            name = part.get_param("name", header="content-disposition")
            if not name:
                continue
            filename = part.get_filename() or ""
            content_type = part.get_content_type()
            payload = part.get_payload(decode=True) or b""
            value = ""
            if not filename:
                charset = part.get_content_charset() or "utf-8"
                value = payload.decode(charset, "replace")
            field = MultipartField(value, filename, content_type, payload)
            if name in form:
                if isinstance(form[name], list):
                    form[name].append(field)
                else:
                    form[name] = [form[name], field]
            else:
                form[name] = field
        return form

    def field_value(self, form, name):
        field = form[name] if name in form else None
        if field is None or isinstance(field, list):
            return ""
        return field.value.strip() if isinstance(field.value, str) else ""

    def field_values(self, form, name):
        field = form[name] if name in form else None
        if field is None:
            return []
        fields = field if isinstance(field, list) else [field]
        return [item.value.strip() for item in fields if isinstance(item.value, str) and item.value.strip()]

    def uploaded_photos(self, form):
        photos = []
        photo_fields = form["photos"] if "photos" in form else []
        if not isinstance(photo_fields, list):
            photo_fields = [photo_fields]
        for photo in photo_fields:
            if not getattr(photo, "filename", ""):
                continue
            content_type = photo.type or ""
            if not content_type.startswith("image/"):
                continue
            extension = Path(photo.filename).suffix.lower()[:12] or ".jpg"
            filename = f"{uuid.uuid4().hex}{extension}"
            target = UPLOAD_DIR / filename
            with target.open("wb") as handle:
                handle.write(photo.file.read())
            photos.append(f"/uploads/{filename}")
        return photos

    def delete_upload(self, url):
        if not url.startswith("/uploads/"):
            return
        filename = Path(unquote(url.removeprefix("/uploads/"))).name
        target = UPLOAD_DIR / filename
        if target.exists():
            target.unlink()

    def product_payload(self, form):
        name = self.field_value(form, "name")
        description = self.field_value(form, "description")
        product_types = [value for value in self.field_values(form, "product_types") if value in PRODUCT_TYPES]
        occasions = [value for value in self.field_values(form, "occasions") if value in OCCASIONS]
        known_color_names = {label for label, _ in KNOWN_COLORS}
        colors = [value for value in self.field_values(form, "colors") if value in known_color_names]
        if not name or not description:
            return None, "Nom et description sont obligatoires."
        if not product_types:
            return None, "Cochez au moins un type de produit."
        if not occasions:
            return None, "Cochez au moins une occasion."
        if not colors:
            return None, "Cochez au moins une couleur."
        return {
            "name": name,
            "description": description,
            "category": product_types[0],
            "product_types": product_types,
            "occasions": occasions,
            "colors": colors,
        }, ""

    def create_product(self):
        if not self.is_admin():
            self.redirect("/admin")
            return
        form = self.multipart_form()
        payload, error = self.product_payload(form)
        if error:
            self.send_html(admin_page(error, "error"), 400)
            return
        photos = self.uploaded_photos(form)
        if not photos:
            self.send_html(admin_page("Ajoutez au moins une photo au produit.", "error"), 400)
            return
        with db() as conn:
            cur = conn.execute(
                """
                INSERT INTO products (name, description, category, product_types, occasions, price, colors, photos)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["name"],
                    payload["description"],
                    payload["category"],
                    json.dumps(payload["product_types"], ensure_ascii=False),
                    json.dumps(payload["occasions"], ensure_ascii=False),
                    0,
                    json.dumps(payload["colors"], ensure_ascii=False),
                    json.dumps(photos),
                ),
            )
        self.send_html(admin_page(f"Produit #{cur.lastrowid} publié.", "success"))

    def update_product(self):
        if not self.is_admin():
            self.redirect("/admin")
            return
        form = self.multipart_form()
        product_id = self.field_value(form, "id")
        product = read_product(product_id)
        if not product:
            self.send_html(admin_page("Produit introuvable.", "error"), 404)
            return
        payload, error = self.product_payload(form)
        if error:
            self.send_html(admin_page(error, "error", product), 400)
            return
        removed = set(self.field_values(form, "remove_photos"))
        photos = [photo for photo in product["photos"] if photo not in removed]
        new_photos = self.uploaded_photos(form)
        photos.extend(new_photos)
        if not photos:
            self.send_html(admin_page("Gardez ou ajoutez au moins une photo.", "error", product), 400)
            return
        for photo in removed:
            self.delete_upload(photo)
        with db() as conn:
            conn.execute(
                """
                UPDATE products
                SET name = ?, description = ?, category = ?, product_types = ?, occasions = ?,
                    price = ?, colors = ?, photos = ?
                WHERE id = ?
                """,
                (
                    payload["name"],
                    payload["description"],
                    payload["category"],
                    json.dumps(payload["product_types"], ensure_ascii=False),
                    json.dumps(payload["occasions"], ensure_ascii=False),
                    0,
                    json.dumps(payload["colors"], ensure_ascii=False),
                    json.dumps(photos),
                    product_id,
                ),
            )
        self.send_html(admin_page(f"Produit #{product_id} modifié.", "success", read_product(product_id)))

    def delete_product(self):
        if not self.is_admin():
            self.redirect("/admin")
            return
        form = self.read_form()
        product_id = form.get("id", "")
        with db() as conn:
            row = conn.execute("SELECT photos FROM products WHERE id = ?", (product_id,)).fetchone()
            if row:
                for photo in json_list(row["photos"]):
                    self.delete_upload(photo)
            conn.execute("DELETE FROM products WHERE id = ?", (product_id,))
        self.redirect("/admin")


class LMFServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


def main():
    init_db()
    port = int(os.environ.get("PORT", "11231"))
    print("Serveur Le Marais Fleuri")
    print(f"URL: http://127.0.0.1:{port}")
    print(f"Passphrase admin temporaire (22 caractères): {ADMIN_PASSPHRASE}")
    print("Elle change à chaque démarrage du serveur.")
    send_admin_passphrase_email()
    LMFServer(("127.0.0.1", port), App).serve_forever()


if __name__ == "__main__":
    main()
