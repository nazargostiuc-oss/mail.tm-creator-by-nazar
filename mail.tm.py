import atexit
import importlib
import json
import math
import os
import random
import re
import shutil
import string
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, asdict, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional


# ════════════════════════════════════════════════════════════════════
#  Зависимости
# ════════════════════════════════════════════════════════════════════
def ensure_deps() -> None:
    missing = []
    for mod, pkg in (("requests", "requests"), ("rich", "rich")):
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(pkg)
    if not missing:
        return
    print("Не хватает библиотек: " + ", ".join(missing))
    ans = input("Установить автоматически? [Y/n]: ").strip().lower()
    if ans in ("", "y", "yes", "д", "да"):
        subprocess.check_call([sys.executable, "-m", "pip", "install", *missing])
    else:
        print("Установи вручную: pip install " + " ".join(missing))
        sys.exit(1)


ensure_deps()

import requests  # noqa: E402
from rich import box  # noqa: E402
from rich.align import Align  # noqa: E402
from rich.console import Console, Group  # noqa: E402
from rich.live import Live  # noqa: E402
from rich.markup import escape  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.progress import (  # noqa: E402
    BarColumn, Progress, SpinnerColumn, TaskProgressColumn,
    TextColumn, TimeElapsedColumn,
)
from rich.prompt import Confirm, IntPrompt, Prompt  # noqa: E402
from rich.table import Table  # noqa: E402
from rich.text import Text  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass


# ════════════════════════════════════════════════════════════════════
#  Константы
# ════════════════════════════════════════════════════════════════════
VERSION = "2.3"
BASE_URL = "https://api.mail.tm"
SITE_URL = "https://mail.tm"
BASE_DIR = Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()
CONFIG_PATH = BASE_DIR / "config.json"
JSON_PATH = BASE_DIR / "accounts.json"

RATE_LIMIT_WAIT = 15.0       # ожидание при 429
RATE_LIMIT_MAX_WAIT = 60.0   # потолок, если Retry-After больше
MAX_429_RETRIES = 10         # только вне автофарма
FARM_MIN_DELAY = 0.5
FARM_MAX_FAILS = 15          # подряд ошибок -> автостоп
JSON_FLUSH_EVERY = 10

USERNAME_STYLES = ["mixed", "words", "letters", "pronounce"]

WORDS = [
    "river", "cloud", "storm", "forest", "silver", "golden", "rocket", "pixel",
    "tiger", "falcon", "ocean", "shadow", "comet", "ember", "frost", "lunar",
    "maple", "nova", "orbit", "panda", "quartz", "raven", "solar", "thunder",
    "ultra", "velvet", "willow", "zephyr", "amber", "breeze", "cobalt", "delta",
    "echo", "flame", "glacier", "harbor", "iris", "jungle", "koala", "lotus",
]

console = Console()


# ════════════════════════════════════════════════════════════════════
#  Исключения
# ════════════════════════════════════════════════════════════════════
class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Cancelled(Exception):
    """Операция прервана (стоп автофарма)."""


# ════════════════════════════════════════════════════════════════════
#  Настройки
# ════════════════════════════════════════════════════════════════════
@dataclass
class Settings:
    default_count: int = 5
    username_length: int = 10
    username_style: str = "mixed"
    password_length: int = 14
    password_special: bool = False
    delay: float = 1.0
    timeout: int = 20
    proxy: str = ""
    txt_file: str = "accounts.txt"
    fetch_token: bool = True
    farm_delay: float = 1.0
    rate_limit_wait: float = RATE_LIMIT_WAIT

    @classmethod
    def load(cls) -> "Settings":
        s = cls()
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            names = {f.name for f in fields(cls)}
            for k, v in data.items():
                if k in names:
                    setattr(s, k, v)
        except Exception:
            pass
        return s

    def save(self) -> None:
        try:
            CONFIG_PATH.write_text(
                json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            console.print(f"[red]Не удалось сохранить настройки: {escape(str(e))}[/]")


# ════════════════════════════════════════════════════════════════════
#  Утилиты
# ════════════════════════════════════════════════════════════════════
def fmt_duration(sec: float) -> str:
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def short_date(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%d.%m %H:%M")
    except Exception:
        return (iso or "")[:16]


def gen_username(style: str, length: int) -> str:
    length = max(4, length)
    letters = string.ascii_lowercase
    digits = string.digits
    if style == "words":
        base = random.choice(WORDS) + random.choice(WORDS)
        return (base + "".join(random.choices(digits, k=4)))[: max(length, len(base))]
    if style == "letters":
        return "".join(random.choices(letters, k=length))
    if style == "pronounce":
        cons, vow = "bcdfghklmnprstvz", "aeiou"
        out = ""
        while len(out) < length - 3:
            out += random.choice(cons) + random.choice(vow)
        return out + "".join(random.choices(digits, k=3))
    first = random.choice(letters)
    return first + "".join(random.choices(letters + digits, k=length - 1))


def gen_password(length: int, special: bool) -> str:
    length = max(8, length)
    pools = [string.ascii_lowercase, string.ascii_uppercase, string.digits]
    if special:
        pools.append("!@#$%^&*-_")
    chars = [random.choice(p) for p in pools]
    allchars = "".join(pools)
    chars += random.choices(allchars, k=length - len(chars))
    random.shuffle(chars)
    return "".join(chars)


def strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</h\d>", "\n", html)
    html = re.sub(r"<[^>]+>", "", html)
    html = (html.replace("&nbsp;", " ").replace("&amp;", "&")
            .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    return re.sub(r"\n\s*\n+", "\n\n", html).strip()


def copy_to_clipboard(text: str) -> bool:
    try:
        if sys.platform.startswith("win"):
            subprocess.run("clip", input=text.encode("utf-16le"), check=True, shell=True)
            return True
        if sys.platform == "darwin":
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
            return True
        for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "-ib"]):
            if shutil.which(cmd[0]):
                subprocess.run(cmd, input=text.encode("utf-8"), check=True)
                return True
    except Exception:
        pass
    try:
        import tkinter
        r = tkinter.Tk()
        r.withdraw()
        r.clipboard_clear()
        r.clipboard_append(text)
        r.update()
        r.destroy()
        return True
    except Exception:
        return False


def ask_int(prompt: str, default: int, lo: int, hi: int) -> int:
    while True:
        try:
            v = IntPrompt.ask(prompt, default=default)
        except Exception:
            v = default
        if lo <= v <= hi:
            return v
        console.print(f"[yellow]Введи число от {lo} до {hi}[/]")


def ask_float(prompt: str, default: float, lo: float, hi: float) -> float:
    while True:
        raw = Prompt.ask(prompt, default=str(default)).replace(",", ".")
        try:
            v = float(raw)
        except ValueError:
            console.print("[yellow]Нужно число[/]")
            continue
        if lo <= v <= hi:
            return v
        console.print(f"[yellow]Допустимо от {lo} до {hi}[/]")


def pause_enter() -> None:
    console.print()
    try:
        Prompt.ask("[dim]Нажми Enter, чтобы вернуться в меню[/]", default="", show_default=False)
    except (KeyboardInterrupt, EOFError):
        pass


# ════════════════════════════════════════════════════════════════════
#  Клиент API
# ════════════════════════════════════════════════════════════════════
class MailTmClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": f"MailTMCreator/{VERSION}",
        })
        self.cancel_event = threading.Event()
        self.on_wait: Optional[Callable[[str, int], None]] = None
        self.farm_mode = False
        self.rate_limit_hits = 0
        self.max_retries = 5
        self.apply_proxy()

    def apply_proxy(self) -> None:
        p = self.settings.proxy.strip()
        self.session.proxies = {"http": p, "https": p} if p else {}

    # ── ожидание ────────────────────────────────────────────────
    def _countdown(self, seconds: float, reason: str) -> None:
        end = time.monotonic() + seconds
        if not self.on_wait:
            console.print(f"[yellow]⏳ {reason}: жду {int(seconds)} с и повторяю…[/]")
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return
            if self.on_wait:
                self.on_wait(reason, int(math.ceil(left)))
            if self.cancel_event.wait(min(0.25, left)):
                raise Cancelled()

    # ── базовый запрос ──────────────────────────────────────────
    def _request(self, method: str, path: str, *, body: Any = None,
                 token: Optional[str] = None, params: Any = None,
                 expected: tuple = (200, 201, 204)) -> Any:
        url = BASE_URL + path
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        net_tries = 0
        rl_tries = 0
        while True:
            if self.cancel_event.is_set():
                raise Cancelled()
            try:
                resp = self.session.request(
                    method, url, json=body, headers=headers, params=params,
                    timeout=self.settings.timeout,
                )
            except requests.RequestException as e:
                net_tries += 1
                if net_tries > self.max_retries:
                    raise ApiError(0, f"Сеть: {e}")
                self._countdown(min(2 ** net_tries, 20), "ошибка сети")
                continue

            code = resp.status_code
            if code == 429:
                self.rate_limit_hits += 1
                rl_tries += 1
                if not self.farm_mode and rl_tries > MAX_429_RETRIES:
                    raise ApiError(429, "Слишком много запросов (429)")
                wait = float(self.settings.rate_limit_wait)
                ra = resp.headers.get("Retry-After")
                if ra:
                    try:
                        wait = max(wait, min(float(ra), RATE_LIMIT_MAX_WAIT))
                    except ValueError:
                        pass
                self._countdown(wait, "лимит API (429)")
                continue

            if 500 <= code < 600:
                net_tries += 1
                if net_tries > self.max_retries:
                    raise ApiError(code, f"Ошибка сервера {code}")
                self._countdown(min(2 ** net_tries, 20), f"сервер {code}")
                continue

            if code in expected:
                if code == 204 or not resp.content:
                    return None
                try:
                    return resp.json()
                except ValueError:
                    return None

            msg = f"HTTP {code}"
            try:
                data = resp.json()
                if isinstance(data, dict):
                    msg = (data.get("hydra:description") or data.get("detail")
                           or data.get("message") or msg)
            except ValueError:
                pass
            raise ApiError(code, str(msg))

    @staticmethod
    def _members(data: Any) -> List[dict]:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("hydra:member") or data.get("member") or []
        return []

    # ── методы API ──────────────────────────────────────────────
    def get_domains(self) -> List[dict]:
        data = self._request("GET", "/domains", params={"page": 1})
        return [d for d in self._members(data) if d.get("isActive", True)]

    def create_account(self, address: str, password: str) -> dict:
        return self._request("POST", "/accounts",
                             body={"address": address, "password": password})

    def get_token(self, address: str, password: str) -> str:
        data = self._request("POST", "/token",
                             body={"address": address, "password": password})
        return data["token"]

    def get_me(self, token: str) -> dict:
        return self._request("GET", "/me", token=token)

    def delete_account(self, acc_id: str, token: str) -> None:
        self._request("DELETE", f"/accounts/{acc_id}", token=token)

    def get_messages(self, token: str) -> List[dict]:
        return self._members(self._request("GET", "/messages", token=token,
                                           params={"page": 1}))

    def get_message(self, token: str, msg_id: str) -> dict:
        return self._request("GET", f"/messages/{msg_id}", token=token)

    def delete_message(self, token: str, msg_id: str) -> None:
        self._request("DELETE", f"/messages/{msg_id}", token=token)


# ════════════════════════════════════════════════════════════════════
#  Хранилище аккаунтов
# ════════════════════════════════════════════════════════════════════
class AccountStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.lock = threading.Lock()
        self.accounts: List[dict] = []
        self.pending = 0
        self.dirty = False
        self.load()

    @property
    def txt_path(self) -> Path:
        p = Path(self.settings.txt_file)
        return p if p.is_absolute() else BASE_DIR / p

    def load(self) -> None:
        with self.lock:
            self.accounts = []
            try:
                data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self.accounts = [a for a in data if isinstance(a, dict) and a.get("email")]
            except Exception:
                pass
            known = {a["email"].lower() for a in self.accounts}
            try:
                for line in self.txt_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if ":" not in line:
                        continue
                    email, pwd = line.split(":", 1)
                    if email.lower() not in known and "@" in email:
                        self.accounts.append({"email": email, "password": pwd, "id": "",
                                              "created": "", "domain": email.split("@")[1]})
                        known.add(email.lower())
                        self.dirty = True
            except FileNotFoundError:
                pass
            except Exception:
                pass
            if self.dirty:
                self._write_json()

    def _write_json(self) -> None:
        tmp = JSON_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.accounts, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, JSON_PATH)
        self.pending = 0
        self.dirty = False

    def add(self, acc: dict) -> None:
        with self.lock:
            if any(a["email"].lower() == acc["email"].lower() for a in self.accounts):
                return
            self.accounts.append(acc)
            try:
                with open(self.txt_path, "a", encoding="utf-8") as f:
                    f.write(f"{acc['email']}:{acc['password']}\n")
                    f.flush()
            except Exception as e:
                console.print(f"[red]Ошибка записи TXT: {escape(str(e))}[/]")
            self.pending += 1
            self.dirty = True
            if self.pending >= JSON_FLUSH_EVERY:
                self._write_json()

    def flush(self) -> None:
        with self.lock:
            if self.dirty or self.pending:
                try:
                    self._write_json()
                except Exception:
                    pass

    def remove_many(self, emails: List[str]) -> None:
        low = {e.lower() for e in emails}
        with self.lock:
            self.accounts = [a for a in self.accounts if a["email"].lower() not in low]
            try:
                with open(self.txt_path, "w", encoding="utf-8") as f:
                    for a in self.accounts:
                        f.write(f"{a['email']}:{a['password']}\n")
            except Exception:
                pass
            self._write_json()

    def snapshot(self) -> List[dict]:
        with self.lock:
            return list(self.accounts)

    def __len__(self) -> int:
        with self.lock:
            return len(self.accounts)


# ════════════════════════════════════════════════════════════════════
#  Создание одного ящика
# ════════════════════════════════════════════════════════════════════
def create_one(client: MailTmClient, store: AccountStore, domains: List[str],
               settings: Settings, verify: bool = True) -> dict:
    last_err: Optional[ApiError] = None
    for _ in range(8):
        domain = random.choice(domains)
        user = gen_username(settings.username_style, settings.username_length)
        address = f"{user}@{domain}"
        password = gen_password(settings.password_length, settings.password_special)
        try:
            data = client.create_account(address, password)
        except ApiError as e:
            if e.status == 422:       # адрес занят / не подошёл домен
                last_err = e
                continue
            raise
        acc = {
            "email": address,
            "password": password,
            "id": (data or {}).get("id", ""),
            "created": now_str(),
            "domain": domain,
        }
        if verify:
            client.get_token(address, password)
        store.add(acc)
        return acc
    raise last_err or ApiError(422, "Не удалось подобрать свободный адрес")


def load_domains(client: MailTmClient) -> List[str]:
    with console.status("[cyan]Получаю список доменов…[/]", spinner="dots"):
        doms = [d["domain"] for d in client.get_domains() if d.get("domain")]
    if not doms:
        raise ApiError(0, "API не вернул ни одного домена")
    return doms


# ════════════════════════════════════════════════════════════════════
#  Оформление
# ════════════════════════════════════════════════════════════════════
GLYPHS = {
    "M": ["███╗   ███╗", "████╗ ████║", "██╔████╔██║", "██║╚██╔╝██║", "██║ ╚═╝ ██║", "╚═╝     ╚═╝"],
    "A": [" █████╗ ", "██╔══██╗", "███████║", "██╔══██║", "██║  ██║", "╚═╝  ╚═╝"],
    "I": ["██╗", "██║", "██║", "██║", "██║", "╚═╝"],
    "L": ["██╗     ", "██║     ", "██║     ", "██║     ", "███████╗", "╚══════╝"],
    "T": ["████████╗", "╚══██╔══╝", "   ██║   ", "   ██║   ", "   ██║   ", "   ╚═╝   "],
    " ": ["    "] * 6,
}
GRADIENT = ["#00f5ff", "#00d4ff", "#4facfe", "#7f7fff", "#b06cff", "#ff6ec7"]


def banner() -> Align:
    word = "MAIL TM"
    lines = []
    for row in range(6):
        lines.append(Text(" ".join(GLYPHS[ch][row] for ch in word),
                          style=f"bold {GRADIENT[row]}"))
    body = Group(*lines)
    sub = Text(f"temporary mail creator  •  v{VERSION}  •  {SITE_URL}",
               style="italic bright_black", justify="center")
    return Align.center(Group(body, Text(""), Align.center(sub)))


def account_card(acc: dict, idx: Optional[int] = None) -> Panel:
    t = Table.grid(padding=(0, 1))
    t.add_column(justify="right", style="bold")
    t.add_column()
    t.add_row("📧 Почта:", Text(acc["email"], style="bold bright_cyan"))
    t.add_row("🔑 Пароль:", Text(acc["password"], style="bold bright_yellow"))
    t.add_row("🌐 Сайт:", Text(SITE_URL, style="underline bright_blue"))
    t.add_row("📋 Строка:", Text(f"{acc['email']}:{acc['password']}", style="bold bright_green"))
    title = f"[bold magenta]Ящик #{idx}[/]" if idx else "[bold magenta]Ящик[/]"
    return Panel(t, title=title, border_style="magenta", box=box.ROUNDED, padding=(0, 2))


def accounts_table(accs: List[dict], title: str = "Мои ящики") -> Table:
    t = Table(title=title, box=box.ROUNDED, header_style="bold cyan",
              border_style="bright_black", title_style="bold magenta")
    t.add_column("#", justify="right", style="dim")
    t.add_column("Почта", style="bright_cyan")
    t.add_column("Пароль", style="bright_yellow")
    t.add_column("Создан", style="dim")
    for i, a in enumerate(accs, 1):
        t.add_row(str(i), Text(a["email"]), Text(a["password"]), a.get("created", "") or "—")
    return t


def show_header() -> None:
    console.clear()
    console.print(banner())
    console.print()


def results_block(accs: List[dict]) -> Panel:
    txt = "\n".join(f"{a['email']}:{a['password']}" for a in accs)
    body = Group(
        Text(txt, style="bold bright_green"),
        Text(""),
        Text(f"🌐 Вход в почту: {SITE_URL}", style="bold bright_blue"),
    )
    return Panel(body, title="[bold green]почта:пароль[/]", border_style="green",
                 box=box.DOUBLE, padding=(1, 2))


def offer_copy(accs: List[dict]) -> None:
    if not accs:
        return
    if Confirm.ask("[cyan]Скопировать почта:пароль в буфер обмена?[/]", default=False):
        txt = "\n".join(f"{a['email']}:{a['password']}" for a in accs)
        if copy_to_clipboard(txt):
            console.print("[green]✔ Скопировано[/]")
        else:
            console.print("[yellow]Буфер недоступен — скопируй вручную из окна[/]")


def pick_account(store: AccountStore, prompt: str = "Номер ящика") -> Optional[dict]:
    accs = store.snapshot()
    if not accs:
        console.print("[yellow]Список ящиков пуст. Сначала создай их (пункт 1 или 9).[/]")
        return None
    console.print(accounts_table(accs))
    n = ask_int(f"[cyan]{prompt}[/] (0 — отмена)", 0, 0, len(accs))
    return accs[n - 1] if n else None


# ════════════════════════════════════════════════════════════════════
#  1. Создание
# ════════════════════════════════════════════════════════════════════
def create_flow(client: MailTmClient, store: AccountStore, settings: Settings) -> None:
    show_header()
    console.print(Panel("[bold]Создание ящиков[/]", border_style="cyan", box=box.ROUNDED))
    count = ask_int("[cyan]Сколько ящиков создать[/]", settings.default_count, 1, 1000)
    try:
        domains = load_domains(client)
    except ApiError as e:
        console.print(f"[red]Ошибка: {escape(e.message)}[/]")
        return pause_enter()

    console.print(f"[green]Доступно доменов:[/] {', '.join(domains)}\n")
    created: List[dict] = []
    errors = 0
    progress = Progress(
        SpinnerColumn(style="magenta"),
        TextColumn("[bold]{task.description}"),
        BarColumn(bar_width=None, complete_style="magenta", finished_style="green"),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console, transient=False,
    )
    task = progress.add_task("Создаю…", total=count)

    def on_wait(reason: str, left: int) -> None:
        progress.update(task, description=f"[yellow]{reason} — повтор через {left} с[/]")

    client.on_wait = on_wait
    try:
        with progress:
            for i in range(1, count + 1):
                progress.update(task, description=f"Создаю ящик {i}/{count}")
                try:
                    acc = create_one(client, store, domains, settings,
                                     verify=settings.fetch_token)
                    created.append(acc)
                    progress.console.print(account_card(acc, i))
                except ApiError as e:
                    errors += 1
                    progress.console.print(f"[red]✖ Ящик {i}: {escape(e.message)}[/]")
                progress.advance(task)
                if i < count and settings.delay > 0:
                    time.sleep(settings.delay)
    except (KeyboardInterrupt, Cancelled):
        console.print("\n[yellow]Прервано пользователем[/]")
    finally:
        client.on_wait = None
        store.flush()

    console.print()
    if created:
        console.print(results_block(created))
    console.print(f"[bold]Создано:[/] [green]{len(created)}[/]   "
                  f"[bold]Ошибок:[/] [red]{errors}[/]   "
                  f"[bold]Файл:[/] {store.txt_path.name}")
    offer_copy(created)
    pause_enter()


# ════════════════════════════════════════════════════════════════════
#  2. Список
# ════════════════════════════════════════════════════════════════════
def list_flow(store: AccountStore) -> None:
    show_header()
    accs = store.snapshot()
    if not accs:
        console.print("[yellow]Пока нет ни одного ящика.[/]")
        return pause_enter()
    console.print(accounts_table(accs, f"Мои ящики ({len(accs)})"))
    console.print(f"\n🌐 Вход: [bold bright_blue]{SITE_URL}[/]   📄 Файл: {store.txt_path}")
    offer_copy(accs)
    pause_enter()


# ════════════════════════════════════════════════════════════════════
#  3. Входящие
# ════════════════════════════════════════════════════════════════════
def read_message(client: MailTmClient, token: str, msg_id: str) -> None:
    try:
        m = client.get_message(token, msg_id)
    except ApiError as e:
        console.print(f"[red]Ошибка: {escape(e.message)}[/]")
        return
    sender = (m.get("from") or {})
    head = Table.grid(padding=(0, 1))
    head.add_column(style="bold cyan", justify="right")
    head.add_column()
    head.add_row("От:", f"{sender.get('name', '')} <{sender.get('address', '')}>")
    head.add_row("Тема:", m.get("subject", "") or "—")
    head.add_row("Дата:", short_date(m.get("createdAt", "")))
    text = (m.get("text") or "").strip()
    if not text:
        html = m.get("html") or []
        text = strip_html("\n".join(html) if isinstance(html, list) else str(html))
    console.print(Panel(head, title="[bold magenta]Письмо[/]", border_style="magenta"))
    console.print(Panel(escape(text or "(пусто)"), border_style="bright_black"))
    links = re.findall(r"https?://[^\s\"'<>\]\)]+", text)
    if links:
        console.print("[bold]Ссылки в письме:[/]")
        for l in list(dict.fromkeys(links))[:10]:
            console.print(f"  [underline bright_blue]{escape(l)}[/]")


def inbox_flow(client: MailTmClient, store: AccountStore) -> None:
    show_header()
    acc = pick_account(store, "Открыть входящие ящика №")
    if not acc:
        return pause_enter()
    try:
        with console.status("[cyan]Вхожу в ящик…[/]", spinner="dots"):
            token = client.get_token(acc["email"], acc["password"])
    except ApiError as e:
        console.print(f"[red]Не удалось войти: {escape(e.message)}[/]")
        return pause_enter()

    while True:
        try:
            msgs = client.get_messages(token)
        except ApiError as e:
            console.print(f"[red]Ошибка: {escape(e.message)}[/]")
            return pause_enter()
        show_header()
        console.print(f"📧 [bold bright_cyan]{escape(acc['email'])}[/]   🌐 {SITE_URL}\n")
        if msgs:
            t = Table(title=f"Входящие ({len(msgs)})", box=box.ROUNDED,
                      header_style="bold cyan", border_style="bright_black")
            t.add_column("#", justify="right", style="dim")
            t.add_column("От", style="bright_cyan")
            t.add_column("Тема", style="bold")
            t.add_column("Дата", style="dim")
            for i, m in enumerate(msgs, 1):
                t.add_row(str(i), (m.get("from") or {}).get("address", "?"),
                          m.get("subject", "") or "—", short_date(m.get("createdAt", "")))
            console.print(t)
        else:
            console.print("[yellow]Писем пока нет.[/]")
        cmd = Prompt.ask("\n[cyan]Номер письма / r — обновить / d N — удалить письмо / 0 — назад[/]",
                         default="r")
        cmd = cmd.strip().lower()
        if cmd == "0":
            return
        if cmd == "r" or cmd == "":
            continue
        if cmd.startswith("d "):
            try:
                n = int(cmd[2:])
                client.delete_message(token, msgs[n - 1]["id"])
                console.print("[green]✔ Удалено[/]")
            except (ValueError, IndexError):
                console.print("[yellow]Неверный номер[/]")
            except ApiError as e:
                console.print(f"[red]{escape(e.message)}[/]")
            time.sleep(0.8)
            continue
        if cmd.isdigit() and 1 <= int(cmd) <= len(msgs):
            read_message(client, token, msgs[int(cmd) - 1]["id"])
            pause_enter()
        else:
            console.print("[yellow]Не понял команду[/]")
            time.sleep(0.8)


# ════════════════════════════════════════════════════════════════════
#  4. Проверка всех
# ════════════════════════════════════════════════════════════════════
def check_flow(client: MailTmClient, store: AccountStore) -> None:
    show_header()
    accs = store.snapshot()
    if not accs:
        console.print("[yellow]Нет ящиков для проверки.[/]")
        return pause_enter()
    ok: List[dict] = []
    dead: List[dict] = []
    progress = Progress(SpinnerColumn(style="magenta"), TextColumn("[bold]Проверяю…"),
                        BarColumn(bar_width=None), TaskProgressColumn(),
                        TimeElapsedColumn(), console=console)
    task = progress.add_task("", total=len(accs))
    client.on_wait = lambda r, left: progress.update(
        task, description=f"[yellow]{r}: {left} с[/]")
    try:
        with progress:
            for a in accs:
                try:
                    token = client.get_token(a["email"], a["password"])
                    n = len(client.get_messages(token))
                    a = dict(a, _msgs=n)
                    ok.append(a)
                except ApiError:
                    dead.append(a)
                progress.advance(task)
                time.sleep(0.2)
    except (KeyboardInterrupt, Cancelled):
        console.print("[yellow]Прервано[/]")
    finally:
        client.on_wait = None

    t = Table(title="Результат проверки", box=box.ROUNDED, header_style="bold cyan")
    t.add_column("Почта", style="bright_cyan")
    t.add_column("Статус")
    t.add_column("Писем", justify="right")
    for a in ok:
        t.add_row(a["email"], "[green]работает[/]", str(a["_msgs"]))
    for a in dead:
        t.add_row(a["email"], "[red]не работает[/]", "—")
    console.print(t)
    console.print(f"\n[green]Рабочих: {len(ok)}[/]   [red]Нерабочих: {len(dead)}[/]")
    if dead and Confirm.ask("[cyan]Удалить нерабочие из списка?[/]", default=False):
        store.remove_many([a["email"] for a in dead])
        console.print("[green]✔ Удалено из списка[/]")
    pause_enter()


# ════════════════════════════════════════════════════════════════════
#  5. Домены
# ════════════════════════════════════════════════════════════════════
def domains_flow(client: MailTmClient) -> None:
    show_header()
    try:
        with console.status("[cyan]Запрашиваю домены…[/]", spinner="dots"):
            doms = client.get_domains()
    except ApiError as e:
        console.print(f"[red]Ошибка: {escape(e.message)}[/]")
        return pause_enter()
    t = Table(title="Доступные домены", box=box.ROUNDED, header_style="bold cyan")
    t.add_column("#", justify="right", style="dim")
    t.add_column("Домен", style="bright_cyan")
    t.add_column("Активен")
    t.add_column("Приватный")
    for i, d in enumerate(doms, 1):
        t.add_row(str(i), d.get("domain", "?"),
                  "[green]да[/]" if d.get("isActive", True) else "[red]нет[/]",
                  "да" if d.get("isPrivate") else "нет")
    console.print(t)
    pause_enter()


# ════════════════════════════════════════════════════════════════════
#  6. Настройки
# ════════════════════════════════════════════════════════════════════
SETTING_ITEMS = [
    ("default_count", "Количество по умолчанию", "int", 1, 1000),
    ("username_length", "Длина имени", "int", 4, 30),
    ("username_style", "Стиль имени", "choice", USERNAME_STYLES),
    ("password_length", "Длина пароля", "int", 8, 64),
    ("password_special", "Спецсимволы в пароле", "bool"),
    ("delay", "Пауза между созданиями, с", "float", 0.0, 60.0),
    ("timeout", "Таймаут запроса, с", "int", 5, 120),
    ("proxy", "Прокси (http://host:port)", "str"),
    ("txt_file", "TXT-файл", "str"),
    ("fetch_token", "Проверять вход после создания", "bool"),
    ("farm_delay", "Пауза автофарма, с", "float", FARM_MIN_DELAY, 60.0),
    ("rate_limit_wait", "Ожидание при 429, с", "float", 1.0, RATE_LIMIT_MAX_WAIT),
]


def settings_flow(settings: Settings, client: MailTmClient, store: AccountStore) -> None:
    while True:
        show_header()
        t = Table(title="Настройки", box=box.ROUNDED, header_style="bold cyan",
                  title_style="bold magenta")
        t.add_column("#", justify="right", style="dim")
        t.add_column("Параметр")
        t.add_column("Значение", style="bright_yellow")
        for i, item in enumerate(SETTING_ITEMS, 1):
            val = getattr(settings, item[0])
            if isinstance(val, bool):
                val = "да" if val else "нет"
            t.add_row(str(i), item[1], str(val) if str(val) != "" else "—")
        console.print(t)
        n = ask_int("\n[cyan]Что изменить[/] (0 — назад)", 0, 0, len(SETTING_ITEMS))
        if n == 0:
            settings.save()
            return
        key, label, kind, *rest = SETTING_ITEMS[n - 1]
        cur = getattr(settings, key)
        if kind == "int":
            setattr(settings, key, ask_int(f"[cyan]{label}[/]", int(cur), rest[0], rest[1]))
        elif kind == "float":
            setattr(settings, key, ask_float(f"[cyan]{label}[/]", float(cur), rest[0], rest[1]))
        elif kind == "bool":
            setattr(settings, key, Confirm.ask(f"[cyan]{label}[/]", default=bool(cur)))
        elif kind == "choice":
            setattr(settings, key,
                    Prompt.ask(f"[cyan]{label}[/]", choices=rest[0], default=str(cur)))
        else:
            v = Prompt.ask(f"[cyan]{label}[/] (пусто — сброс)", default=str(cur) or "")
            setattr(settings, key, v.strip())
        settings.save()
        client.apply_proxy()


# ════════════════════════════════════════════════════════════════════
#  7. Удаление
# ════════════════════════════════════════════════════════════════════
def delete_flow(client: MailTmClient, store: AccountStore) -> None:
    show_header()
    accs = store.snapshot()
    if not accs:
        console.print("[yellow]Список пуст.[/]")
        return pause_enter()
    console.print(accounts_table(accs))
    raw = Prompt.ask("\n[cyan]Номера через запятую, 'all' — все, пусто — отмена[/]",
                     default="").strip().lower()
    if not raw:
        return
    if raw == "all":
        chosen = accs
    else:
        idx = set()
        for p in raw.replace(" ", "").split(","):
            if p.isdigit() and 1 <= int(p) <= len(accs):
                idx.add(int(p) - 1)
        chosen = [accs[i] for i in sorted(idx)]
    if not chosen:
        console.print("[yellow]Ничего не выбрано.[/]")
        return pause_enter()
    if not Confirm.ask(f"[red]Удалить {len(chosen)} ящ. и с сервера, и из списка?[/]",
                       default=False):
        return
    removed = []
    try:
        for a in chosen:
            try:
                token = client.get_token(a["email"], a["password"])
                acc_id = a.get("id") or client.get_me(token).get("id", "")
                if acc_id:
                    client.delete_account(acc_id, token)
                console.print(f"[green]✔[/] {escape(a['email'])}")
            except ApiError as e:
                console.print(f"[yellow]⚠ {escape(a['email'])}: {escape(e.message)} "
                              f"(убираю только из списка)[/]")
            removed.append(a["email"])
            time.sleep(0.3)
    except (KeyboardInterrupt, Cancelled):
        console.print("[yellow]Прервано[/]")
    store.remove_many(removed)
    console.print(f"\n[green]Убрано из списка: {len(removed)}[/]")
    pause_enter()


# ════════════════════════════════════════════════════════════════════
#  8. О программе
# ════════════════════════════════════════════════════════════════════
def about_flow(settings: Settings, store: AccountStore) -> None:
    show_header()
    t = Table.grid(padding=(0, 2))
    t.add_column(style="bold cyan", justify="right")
    t.add_column()
    t.add_row("Версия:", VERSION)
    t.add_row("API:", BASE_URL)
    t.add_row("Вход в почту:", SITE_URL)
    t.add_row("Ящиков в базе:", str(len(store)))
    t.add_row("TXT-файл:", str(store.txt_path))
    t.add_row("JSON-файл:", str(JSON_PATH))
    t.add_row("Конфиг:", str(CONFIG_PATH))
    t.add_row("429 → ожидание:", f"{settings.rate_limit_wait:g} с")
    console.print(Panel(t, title="[bold magenta]О программе[/]", border_style="magenta"))
    console.print(Panel(
        "• Автофарм (пункт 9) создаёт ящики без остановки; стоп — Q / Enter / Space / Ctrl+C.\n"
        "• TXT пишется мгновенно после каждого ящика, JSON — каждые 10 и при выходе.\n"
        "• При 429 программа ждёт 10 с и повторяет тот же запрос.\n"
        "• Если 429 часто — увеличь паузу автофарма до 2–3 с (настройка 11).",
        title="Советы", border_style="bright_black"))
    pause_enter()


# ════════════════════════════════════════════════════════════════════
#  9. Автофарм
# ════════════════════════════════════════════════════════════════════
class StopListener(threading.Thread):
    """Ждёт нажатия Q / Й / Enter / Space и выставляет stop_event."""

    STOP_KEYS = ("q", "Q", "й", "Й", "\r", "\n", " ", "\x1b")

    def __init__(self, stop_event: threading.Event):
        super().__init__(daemon=True)
        self.stop_event = stop_event
        self._quit = threading.Event()

    def run(self) -> None:
        try:
            if os.name == "nt":
                self._run_windows()
            else:
                self._run_unix()
        except Exception:
            pass

    def _run_windows(self) -> None:
        import msvcrt
        while not self._quit.is_set():
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):
                    msvcrt.getwch()
                    continue
                if ch in self.STOP_KEYS:
                    self.stop_event.set()
                    return
            else:
                time.sleep(0.05)

    def _run_unix(self) -> None:
        import select
        import termios
        import tty
        fd = sys.stdin.fileno()
        try:
            old = termios.tcgetattr(fd)
        except Exception:
            return
        try:
            tty.setcbreak(fd)
            while not self._quit.is_set():
                r, _, _ = select.select([fd], [], [], 0.1)
                if r:
                    ch = os.read(fd, 1).decode("utf-8", errors="ignore")
                    if ch in self.STOP_KEYS:
                        self.stop_event.set()
                        return
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    def shutdown(self) -> None:
        self._quit.set()
        self.join(timeout=1.5)
        try:
            if os.name == "nt":
                import msvcrt
                while msvcrt.kbhit():
                    msvcrt.getwch()
            else:
                import termios
                termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
        except Exception:
            pass


class FarmDashboard:
    def __init__(self, client: MailTmClient, store: AccountStore, pause: float,
                 max_count: int):
        self.client = client
        self.store = store
        self.pause = pause
        self.max_count = max_count
        self.lock = threading.Lock()
        self.created = 0
        self.errors = 0
        self.started = time.time()
        self.status = "запуск…"
        self.last: Deque[dict] = deque(maxlen=6)
        self.log: Deque[str] = deque(maxlen=3)

    def set_status(self, s: str) -> None:
        with self.lock:
            self.status = s

    def add_log(self, s: str) -> None:
        with self.lock:
            self.log.append(f"[dim]{datetime.now().strftime('%H:%M:%S')}[/] {s}")

    def add_account(self, acc: dict) -> None:
        with self.lock:
            self.created += 1
            self.last.append(acc)

    def add_error(self) -> None:
        with self.lock:
            self.errors += 1

    def render(self) -> Group:
        with self.lock:
            elapsed = time.time() - self.started
            rate = self.created / elapsed * 60 if elapsed > 1 else 0.0
            stats = Table.grid(padding=(0, 3))
            for _ in range(4):
                stats.add_column()
            stats.add_row(
                f"[bold]✅ Создано:[/] [bright_green]{self.created}"
                + (f"/{self.max_count}" if self.max_count else "") + "[/]",
                f"[bold]❌ Ошибок:[/] [red]{self.errors}[/]",
                f"[bold]⏱ Время:[/] {fmt_duration(elapsed)}",
                f"[bold]⚡ В минуту:[/] {rate:.1f}",
            )
            stats.add_row(
                f"[bold]🚦 429:[/] [yellow]{self.client.rate_limit_hits}[/]",
                f"[bold]⏸ Пауза:[/] {self.pause:g} с",
                f"[bold]📄 Файл:[/] {self.store.txt_path.name}",
                "",
            )
            status = Text.from_markup(f"[bold]Статус:[/] [bright_magenta]{escape(self.status)}[/]")

            last = Table(box=box.SIMPLE_HEAD, header_style="bold cyan", expand=True)
            last.add_column("#", justify="right", style="dim", width=5)
            last.add_column("почта:пароль", style="bright_green")
            base = self.created - len(self.last)
            for i, a in enumerate(self.last, 1):
                last.add_row(str(base + i), Text(f"{a['email']}:{a['password']}"))
            if not self.last:
                last.add_row("—", "[dim]ещё нет ящиков[/]")

            log_text = Text.from_markup("\n".join(self.log) if self.log else "[dim]журнал пуст[/]")
            hint = Align.center(Text(
                "Нажми Q / Enter / Space (или Ctrl+C), чтобы остановить  •  "
                f"вход в почту: {SITE_URL}", style="bold black on bright_yellow"))

            return Group(
                Panel(Group(stats, Text(""), status), title="[bold magenta]🚜 АВТОФАРМ[/]",
                      border_style="magenta", box=box.ROUNDED),
                Panel(last, title="Последние ящики", border_style="green", box=box.ROUNDED),
                Panel(log_text, title="Журнал", border_style="bright_black", box=box.ROUNDED),
                hint,
            )


def farm_flow(client: MailTmClient, store: AccountStore, settings: Settings) -> None:
    show_header()
    console.print(Panel(
        "[bold]Автофарм[/] создаёт ящики непрерывно.\n"
        "Остановка: [bold]Q[/] / [bold]Enter[/] / [bold]Space[/] / [bold]Ctrl+C[/] или закрой окно.\n"
        f"При ошибке 429 — ждёт [bold]{settings.rate_limit_wait:g} с[/] и повторяет.\n"
        "Каждый ящик сразу пишется в TXT.",
        border_style="cyan", box=box.ROUNDED))
    pause = ask_float("[cyan]Пауза между ящиками, с[/]", max(settings.farm_delay, FARM_MIN_DELAY),
                      FARM_MIN_DELAY, 60.0)
    max_count = ask_int("[cyan]Сколько создать максимум[/] (0 — бесконечно)", 0, 0, 10_000_000)
    try:
        domains = load_domains(client)
    except ApiError as e:
        console.print(f"[red]Ошибка: {escape(e.message)}[/]")
        return pause_enter()
    console.print(f"[green]Домены:[/] {', '.join(domains)}")
    if not Confirm.ask("[bold cyan]Запускаем?[/]", default=True):
        return

    stop = threading.Event()
    client.cancel_event = stop
    client.farm_mode = True
    dash = FarmDashboard(client, store, pause, max_count)
    client.on_wait = lambda reason, left: dash.set_status(f"{reason} — повтор через {left} с")
    session: List[dict] = []
    reason = "остановлено пользователем"
    fails = 0
    listener = StopListener(stop)
    listener.start()
    try:
        with Live(get_renderable=dash.render, console=console, refresh_per_second=4,
                  transient=False):
            while not stop.is_set():
                if max_count and dash.created >= max_count:
                    reason = "достигнут заданный лимит"
                    break
                dash.set_status("создаю ящик…")
                try:
                    acc = create_one(client, store, domains, settings, verify=False)
                except Cancelled:
                    break
                except ApiError as e:
                    fails += 1
                    dash.add_error()
                    dash.add_log(f"[red]ошибка:[/] {escape(e.message)} ({fails}/{FARM_MAX_FAILS})")
                    if fails >= FARM_MAX_FAILS:
                        reason = f"{FARM_MAX_FAILS} ошибок подряд — автостоп"
                        break
                    wait = min(5 * fails, 30)
                    dash.set_status(f"ошибка, пауза {wait} с")
                    if stop.wait(wait):
                        break
                    continue
                fails = 0
                session.append(acc)
                dash.add_account(acc)
                dash.add_log(f"[green]+[/] {escape(acc['email'])}")
                dash.set_status(f"пауза {pause:g} с")
                if stop.wait(pause):
                    break
    except KeyboardInterrupt:
        reason = "остановлено (Ctrl+C)"
    finally:
        stop.set()
        listener.shutdown()
        client.on_wait = None
        client.farm_mode = False
        client.cancel_event = threading.Event()
        store.flush()

    farm_summary(dash, session, reason, store)


def farm_summary(dash: FarmDashboard, session: List[dict], reason: str,
                 store: AccountStore) -> None:
    elapsed = time.time() - dash.started
    t = Table.grid(padding=(0, 2))
    t.add_column(style="bold cyan", justify="right")
    t.add_column()
    t.add_row("Причина остановки:", reason)
    t.add_row("Создано ящиков:", f"[bright_green]{dash.created}[/]")
    t.add_row("Ошибок:", f"[red]{dash.errors}[/]")
    t.add_row("Ответов 429:", f"[yellow]{dash.client.rate_limit_hits}[/]")
    t.add_row("Время работы:", fmt_duration(elapsed))
    t.add_row("Файл:", str(store.txt_path))
    t.add_row("Сайт для входа:", f"[underline bright_blue]{SITE_URL}[/]")
    console.print()
    console.print(Panel(t, title="[bold green]Итоги автофарма[/]", border_style="green",
                        box=box.DOUBLE))
    if session:
        tail = session[-10:]
        console.print(results_block(tail))
        if len(session) > len(tail):
            console.print(f"[dim]Показаны последние {len(tail)} из {len(session)}. "
                          f"Все — в {store.txt_path.name}[/]")
        offer_copy(session)
    pause_enter()


# ════════════════════════════════════════════════════════════════════
#  Главное меню
# ════════════════════════════════════════════════════════════════════
MENU = [
    ("1", "📧", "Создать ящики"),
    ("2", "📂", "Мои ящики"),
    ("3", "📬", "Входящие письма"),
    ("4", "🔍", "Проверить все ящики"),
    ("5", "🌍", "Домены"),
    ("6", "⚙️ ", "Настройки"),
    ("7", "🗑️ ", "Удалить ящики"),
    ("8", "ℹ️ ", "О программе"),
    ("9", "🚜", "АВТОФАРМ (бесконечно)"),
    ("0", "🚪", "Выход"),
]


def draw_menu(store: AccountStore) -> None:
    show_header()
    t = Table.grid(padding=(0, 2))
    t.add_column(justify="right", style="bold bright_magenta")
    t.add_column()
    t.add_column(style="bold")
    for key, icon, label in MENU:
        style = "bold bright_yellow" if key == "9" else ""
        t.add_row(f"[{key}]", icon, Text(label, style=style))
    console.print(Align.center(Panel(t, title="[bold cyan]Главное меню[/]",
                                     border_style="cyan", box=box.ROUNDED,
                                     padding=(1, 4), expand=False)))
    console.print(Align.center(Text(
        f"Ящиков в базе: {len(store)}   •   вход в почту: {SITE_URL}", style="dim")))
    console.print()


def main() -> None:
    settings = Settings.load()
    store = AccountStore(settings)
    client = MailTmClient(settings)
    atexit.register(store.flush)

    while True:
        draw_menu(store)
        try:
            choice = Prompt.ask("[bold cyan]Выбери пункт[/]",
                                choices=[m[0] for m in MENU], show_choices=False)
        except (KeyboardInterrupt, EOFError):
            choice = "0"
        try:
            if choice == "1":
                create_flow(client, store, settings)
            elif choice == "2":
                list_flow(store)
            elif choice == "3":
                inbox_flow(client, store)
            elif choice == "4":
                check_flow(client, store)
            elif choice == "5":
                domains_flow(client)
            elif choice == "6":
                settings_flow(settings, client, store)
            elif choice == "7":
                delete_flow(client, store)
            elif choice == "8":
                about_flow(settings, store)
            elif choice == "9":
                farm_flow(client, store, settings)
            elif choice == "0":
                store.flush()
                console.print("\n[bold magenta]Пока! Все ящики сохранены.[/]\n")
                return
        except KeyboardInterrupt:
            console.print("\n[yellow]Отменено[/]")
            store.flush()
            time.sleep(0.6)
        except Exception as e:  # не даём программе упасть из-за одной ошибки
            console.print(f"[red]Непредвиденная ошибка: {escape(repr(e))}[/]")
            store.flush()
            pause_enter()


if __name__ == "__main__":
    main()