from fastapi import FastAPI, Form, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import (
    create_engine, Column, Integer, String, Boolean,
    Date, DateTime, ForeignKey, text, or_
)
from sqlalchemy.orm import sessionmaker, declarative_base, relationship, Session
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote
import uvicorn
import io
import os
import sys
import socket
import qrcode
import json

# 標準出力が日本語を扱えない環境向けにUTF-8・行バッファリングを強制
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

APP_VERSION = "1.0.14"

JST = ZoneInfo("Asia/Tokyo")


def now_jst() -> datetime:
    # SQLite は DateTime のタイムゾーン情報を保持しないため、
    # ここでは「日本時間の値を持つnaiveなdatetime」として統一する
    # （tz-awareとnaiveの引き算エラーを避けるため）
    return datetime.now(JST).replace(tzinfo=None)


def today_jst() -> date:
    return now_jst().date()


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

# Renderの永続ディスクなど、DBを保存する場所を環境変数で指定できるようにする。
# 未設定の場合は main.py と同じフォルダに保存する（ローカル開発用）。
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)
DATABASE_URL = f"sqlite:///{os.path.join(DATA_DIR, 'dojo.db')}"


def get_local_lan_ip() -> str:
    """同じWi-Fi上のスマホからアクセスできるよう、このPCのLAN内IPを推測する。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "localhost"
    finally:
        s.close()


def get_base_url() -> str:
    """公開URL（例: https://xxx.onrender.com）を返す。
    Renderはウェブサービスに RENDER_EXTERNAL_URL を自動設定する。
    それが無い場合は PUBLIC_BASE_URL を手動設定するか、
    ローカル開発ではスマホからも開けるようLAN内IPを使う。"""
    url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("PUBLIC_BASE_URL")
    if url:
        return url.rstrip("/")
    port = os.environ.get("PORT", "8000")
    return f"http://{get_local_lan_ip()}:{port}"


engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

app = FastAPI()
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/manifest.json")
def manifest():
    return JSONResponse({
        "name": "キックボクシング エキスパートクラス",
        "short_name": "Kickbox",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#f2f2f2",
        "theme_color": "#4a90e2",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"}
        ]
    })


@app.get("/sw.js")
def service_worker():
    js = """
    self.addEventListener('install', () => self.skipWaiting());
    self.addEventListener('activate', () => self.clients.claim());
    self.addEventListener('fetch', (event) => {
        event.respondWith(fetch(event.request));
    });
    """
    return Response(content=js, media_type="application/javascript")

# -----------------------
# DB MODELS
# -----------------------

class Member(Base):
    __tablename__ = "members"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    created_at = Column(DateTime, default=now_jst)


class TodaySparring(Base):
    __tablename__ = "today_sparring"
    id = Column(Integer, primary_key=True, index=True)
    member_id = Column(Integer, ForeignKey("members.id"))
    join_type = Column(String)  # "on_time" or "late"
    late_time = Column(String, nullable=True)
    date = Column(Date, default=today_jst)
    joined_at = Column(DateTime, nullable=True)  # 実際に参加登録した日本時間
    member = relationship("Member")


class SparringSettings(Base):
    __tablename__ = "sparring_settings"
    id = Column(Integer, primary_key=True, index=True)
    number_of_pairs = Column(Integer)
    round_duration = Column(String)
    number_of_rounds = Column(Integer)
    max_skip_rounds = Column(Integer, default=2)
    break_duration = Column(String, default="00:30")


class SparringGroup(Base):
    __tablename__ = "sparring_groups"
    id = Column(Integer, primary_key=True, index=True)
    round_number = Column(Integer)
    pair_number = Column(Integer)
    member_a_id = Column(Integer, ForeignKey("members.id"), nullable=True)
    member_b_id = Column(Integer, ForeignKey("members.id"), nullable=True)
    waiting_member_id = Column(Integer, ForeignKey("members.id"), nullable=True)
    is_waiting = Column(Boolean, default=False)
    remaining_time = Column(String, nullable=True)
    date = Column(Date, default=today_jst)

    member_a = relationship("Member", foreign_keys=[member_a_id])
    member_b = relationship("Member", foreign_keys=[member_b_id])
    waiting_member = relationship("Member", foreign_keys=[waiting_member_id])


class SparringState(Base):
    __tablename__ = "sparring_state"
    id = Column(Integer, primary_key=True, index=True)
    date = Column(Date, default=today_jst)
    current_round = Column(Integer, default=1)
    round_started_at = Column(DateTime, nullable=True)
    is_running = Column(Boolean, default=False)
    is_paused = Column(Boolean, default=False)
    paused_remaining_seconds = Column(Integer, nullable=True)


Base.metadata.create_all(bind=engine)

# 既存DBに新しいカラムを安全に追加（マイグレーション）
with engine.connect() as conn:
    existing_columns = [row[1] for row in conn.execute(text("PRAGMA table_info(sparring_settings)"))]
    if "max_skip_rounds" not in existing_columns:
        conn.execute(text("ALTER TABLE sparring_settings ADD COLUMN max_skip_rounds INTEGER DEFAULT 2"))
        conn.commit()
    if "break_duration" not in existing_columns:
        conn.execute(text("ALTER TABLE sparring_settings ADD COLUMN break_duration TEXT DEFAULT '00:30'"))
        conn.commit()

    existing_ts_columns = [row[1] for row in conn.execute(text("PRAGMA table_info(today_sparring)"))]
    if "joined_at" not in existing_ts_columns:
        conn.execute(text("ALTER TABLE today_sparring ADD COLUMN joined_at DATETIME"))
        conn.commit()

    existing_state_columns = [row[1] for row in conn.execute(text("PRAGMA table_info(sparring_state)"))]
    if "is_paused" not in existing_state_columns:
        conn.execute(text("ALTER TABLE sparring_state ADD COLUMN is_paused BOOLEAN DEFAULT 0"))
        conn.commit()
    if "paused_remaining_seconds" not in existing_state_columns:
        conn.execute(text("ALTER TABLE sparring_state ADD COLUMN paused_remaining_seconds INTEGER"))
        conn.commit()

# -----------------------
# DB SESSION
# -----------------------

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_or_create_state(db: Session, today: date) -> SparringState:
    state = db.query(SparringState).filter(SparringState.date == today).first()
    if not state:
        state = SparringState(date=today, current_round=1, round_started_at=None, is_running=False)
        db.add(state)
        db.commit()
        db.refresh(state)
    return state


# -----------------------
# インストラクターページのパスワード保護
# -----------------------

ADMIN_PASSWORD = "cave"


class NotAuthenticated(Exception):
    pass


@app.exception_handler(NotAuthenticated)
def not_authenticated_handler(request: Request, exc: NotAuthenticated):
    return RedirectResponse(url="/admin/login", status_code=303)


def require_admin(request: Request):
    # その日のうちは再入力不要。日付が変わったら再度パスワードを求める
    if request.cookies.get("admin_auth") != str(today_jst()):
        raise NotAuthenticated()


# 名前未入力のまま送信された場合に「お名前を確認してください」とポップアップ表示する
NAME_VALIDATION_ATTRS = (
    'oninvalid="this.setCustomValidity(\'お名前を確認してください\')" '
    'oninput="this.setCustomValidity(\'\')"'
)


def time_picker(name: str, first_default: int = 19, second_default: int = 0,
                 first_max: int = 23, first_label: str = "時", second_label: str = "分",
                 min_hour: int = None, min_minute: int = 0) -> str:
    # min_hourを指定すると、それより前の時刻はドロップダウンの選択肢自体に出さない
    first_range = range(min_hour, first_max + 1) if min_hour is not None else range(first_max + 1)
    first_options = "".join(
        f'<option value="{i:02d}"{" selected" if i == first_default else ""}>{i:02d}</option>'
        for i in first_range
    )
    # 分の選択肢は常に0〜59を出しておき、選んだ時が最短の時と同じ場合だけ
    # JS側で現在時刻より前の分を除外する（それより後の時を選べば全分が使える）
    second_options = "".join(
        f'<option value="{i:02d}"{" selected" if i == second_default else ""}>{i:02d}</option>'
        for i in range(60)
    )
    min_attrs = f' data-min-hour="{min_hour}" data-min-minute="{min_minute}"' if min_hour is not None else ""
    onchange_attr = f' onchange="restrictMinutes_{name}()"' if min_hour is not None else ""
    restrict_script = f"""
    <script>
        (function() {{
            function restrictMinutes_{name}() {{
                const hourSelect = document.getElementById('{name}_a');
                const minuteSelect = document.getElementById('{name}_b');
                if (!hourSelect || !minuteSelect || !hourSelect.dataset.minHour) return;
                const minHour = parseInt(hourSelect.dataset.minHour, 10);
                const minMinute = parseInt(hourSelect.dataset.minMinute, 10);
                const selectedHour = parseInt(hourSelect.value, 10);
                const currentVal = minuteSelect.value;
                const floor = (selectedHour === minHour) ? minMinute : 0;
                let html = '';
                for (let m = floor; m < 60; m++) {{
                    const v = String(m).padStart(2, '0');
                    html += '<option value="' + v + '"' + (v === currentVal ? ' selected' : '') + '>' + v + '</option>';
                }}
                minuteSelect.innerHTML = html;
                if (!Array.from(minuteSelect.options).some(function(o) {{ return o.value === currentVal; }})) {{
                    minuteSelect.selectedIndex = 0;
                }}
            }}
            window.restrictMinutes_{name} = restrictMinutes_{name};
            restrictMinutes_{name}();
        }})();
    </script>
    """ if min_hour is not None else ""
    return f"""
    <select name="{name}_a" id="{name}_a"{min_attrs}{onchange_attr}>{first_options}</select> {first_label}
    <select name="{name}_b" id="{name}_b">{second_options}</select> {second_label}
    {restrict_script}
    """

# -----------------------
# HTML TEMPLATE (DESIGN)
# -----------------------

def html_page(title: str, body: str, back_url: str = None, show_member_qr: bool = False,
              admin_round_watcher: bool = False, member_qr_size: int = 80, wide: bool = False,
              initial_round: int = None, initial_paused: bool = None, initial_running: bool = None) -> HTMLResponse:
    back_button_js = f"location.href='{back_url}'" if back_url else "history.back()"
    card_max_width = "1300px" if wide else "600px"
    member_qr_html = f"""
    <div id="member-qr-box" style="position:fixed; top:10px; right:10px; text-align:center; z-index:500;">
        <img src="/qr/member" width="{member_qr_size}" height="{member_qr_size}" alt="Member QR" style="border-radius:6px; box-shadow:0 2px 6px rgba(0,0,0,0.15);" /><br>
        <span style="font-size:11px; color:#999;">メンバー参加用</span>
    </div>
    <script>
        (function() {{
            try {{
                // スマホなど狭い画面では、明示的に「表示」に切り替えていない限り
                // 常時表示のQRコードは邪魔になるためデフォルトで非表示にする
                const pref = localStorage.getItem('member_qr_hidden');
                const shouldHide = pref === '1' || (pref === null && window.innerWidth < 700);
                if (shouldHide) {{
                    document.addEventListener('DOMContentLoaded', function() {{
                        const box = document.getElementById('member-qr-box');
                        if (box) box.style.display = 'none';
                    }});
                }}
            }} catch (e) {{}}
        }})();
        function toggleMemberQr() {{
            const box = document.getElementById('member-qr-box');
            if (!box) return;
            const hidden = box.style.display === 'none';
            box.style.display = hidden ? '' : 'none';
            try {{ localStorage.setItem('member_qr_hidden', hidden ? '0' : '1'); }} catch (e) {{}}
        }}
    </script>
    """ if show_member_qr else ""
    initial_state_js = f"""
    <script>
        window.__initialRound = {initial_round if initial_round is not None else 'null'};
        window.__initialPaused = {'true' if initial_paused else ('false' if initial_paused is not None else 'null')};
        window.__initialRunning = {'true' if initial_running else ('false' if initial_running is not None else 'null')};
    </script>
    """ if admin_round_watcher else ""
    round_watcher_html = initial_state_js + """
    <div class="popup-overlay" id="round-watch-popup">
        <div class="popup-box">
            <p id="round-watch-title">ラウンド終了！</p>
            <div id="round-watch-next-pairs"></div>
            <p class="btn-sub" style="display:block;"><span id="round-watch-countdown"></span>秒操作がなければ自動で進みます</p>
            <div style="display:flex; gap:10px; justify-content:center;">
                <button onclick="advanceRoundFromWatcher()">Yes</button>
                <button onclick="declineRoundFromWatcher()" style="background:#999;">No</button>
            </div>
        </div>
    </div>
    <script>
        let breakCountdownInterval = null;
        function stopWatcherCountdown() {
            if (breakCountdownInterval) {
                clearInterval(breakCountdownInterval);
                breakCountdownInterval = null;
            }
            document.getElementById('round-watch-popup').style.display = 'none';
        }
        function advanceRoundFromWatcher() {
            stopWatcherCountdown();
            fetch('/admin/status/next_round', { method: 'POST' }).catch(function() {});
        }
        function declineRoundFromWatcher() {
            // 「いいえ」は一時停止として扱い、他の端末のカウントダウンも同期して止める
            stopWatcherCountdown();
            fetch('/admin/status/pause_round', { method: 'POST' }).catch(function() {});
        }
        (function() {
            // スパーリング状況ページを見ている端末同士で、一時停止・再開・ラウンド進行を
            // 数秒以内に同期する（他の管理ページでは邪魔になるので対象外）
            const onStatusPage = window.location.pathname === '/admin/status';
            let lastRound = (typeof window.__initialRound === 'number') ? window.__initialRound : null;
            let lastPaused = (typeof window.__initialPaused === 'boolean') ? window.__initialPaused : null;
            let lastRunning = (typeof window.__initialRunning === 'boolean') ? window.__initialRunning : null;
            function pollRoundStatus() {
                fetch('/admin/status/poll').then(function(r) { return r.json(); }).then(function(data) {
                    if (!data.has_session) return;
                    if (onStatusPage) {
                        if (lastRound === null) {
                            lastRound = data.current_round;
                            lastPaused = data.is_paused;
                            lastRunning = data.is_running;
                        } else if (data.current_round !== lastRound || data.is_paused !== lastPaused || data.is_running !== lastRunning) {
                            window.location.reload();
                            return;
                        }
                        // スパーリング状況ページには専用の終了ポップアップが既にあるため、
                        // ここでは同期のみ行い、別の通知は出さない
                        return;
                    }
                    // 誰かが既に一時停止・進行させた場合はポップアップとカウントダウンを止める
                    if (data.is_paused || !data.is_running) {
                        stopWatcherCountdown();
                        return;
                    }
                    if (data.is_running && !data.is_paused && data.round_started_at) {
                        const startTime = new Date(data.round_started_at);
                        const elapsed = Math.floor((Date.now() - startTime.getTime()) / 1000);
                        const remaining = data.round_duration_seconds - elapsed;
                        if (remaining <= 0) {
                            document.getElementById('round-watch-title').innerText = 'ラウンド' + data.current_round + '終了！';
                            document.getElementById('round-watch-next-pairs').innerHTML = data.next_pairs_html;
                            document.getElementById('round-watch-popup').style.display = 'block';
                            // 既にカウントダウン中なら二重に始めない。他の端末が対応するまで繰り返し表示し続ける
                            if (!breakCountdownInterval) {
                                let remainingBreak = data.break_duration_seconds || 30;
                                const el = document.getElementById('round-watch-countdown');
                                if (el) el.innerText = remainingBreak;
                                breakCountdownInterval = setInterval(function() {
                                    remainingBreak -= 1;
                                    if (el) el.innerText = Math.max(remainingBreak, 0);
                                    if (remainingBreak <= 0) {
                                        clearInterval(breakCountdownInterval);
                                        breakCountdownInterval = null;
                                        advanceRoundFromWatcher();
                                    }
                                }, 1000);
                            }
                        } else {
                            stopWatcherCountdown();
                        }
                    }
                }).catch(function() {});
            }
            setInterval(pollRoundStatus, 3000);
        })();
    </script>
    """ if admin_round_watcher else ""
    return HTMLResponse(f"""
    <html>
    <head>
        <title>{title}</title>
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <link rel="manifest" href="/manifest.json" />
        <meta name="theme-color" content="#4a90e2" />
        <link rel="apple-touch-icon" href="/static/icon-192.png" />
        <meta name="apple-mobile-web-app-capable" content="yes" />
        <meta name="apple-mobile-web-app-status-bar-style" content="default" />
        <meta name="apple-mobile-web-app-title" content="Kickbox" />
        <style>
            body {{
                font-family: 'Yu Gothic', 'Meiryo', sans-serif;
                background: #f2f2f2;
                margin: 0;
                padding: 20px;
                text-align: center;
            }}
            h1 {{
                color: #333;
                margin-bottom: 20px;
            }}
            .card {{
                background-image: linear-gradient(rgba(255,255,255,0.35), rgba(255,255,255,0.35)), url('/static/welcome-bg.webp');
                background-size: cover;
                background-position: center;
                background-color: white;
                padding: 20px;
                margin: 20px auto;
                width: 80%;
                max-width: {card_max_width};
                border-radius: 12px;
                box-shadow: 0 4px 10px rgba(0,0,0,0.1);
            }}
            .pair-box {{
                background: #4a90e2;
                color: white;
                padding: 15px;
                margin: 10px auto;
                border-radius: 10px;
                width: 70%;
                font-size: 20px;
                font-weight: bold;
            }}
            .wait-box {{
                background: #bfbfbf;
                color: #333;
                padding: 15px;
                margin: 10px auto;
                border-radius: 10px;
                width: 70%;
                font-size: 20px;
                font-weight: bold;
            }}
            .round-overview {{
                background: #f2f2f2;
                border-radius: 10px;
                padding: 10px;
                margin: 8px 0;
                text-align: left;
            }}
            .round-overview h4 {{
                margin: 0 0 8px 6px;
                font-size: 15px;
                color: #555;
            }}
            .pair-chip-row {{
                display: flex;
                flex-wrap: wrap;
                gap: 6px;
            }}
            .pair-chip {{
                background: #4a90e2;
                color: white;
                padding: 8px 10px;
                border-radius: 8px;
                font-size: 13px;
                font-weight: bold;
                flex: 1 1 calc(25% - 6px);
                min-width: 110px;
                text-align: center;
                box-sizing: border-box;
            }}
            button {{
                background: #4a90e2;
                color: white;
                padding: 10px 20px;
                border: none;
                border-radius: 8px;
                font-size: 18px;
                cursor: pointer;
                margin: 10px;
            }}
            button:hover {{
                background: #357ABD;
            }}
            input {{
                padding: 10px;
                font-size: 18px;
                border-radius: 8px;
                border: 1px solid #ccc;
                width: 60%;
            }}
            select {{
                padding: 10px;
                font-size: 18px;
                border-radius: 8px;
                border: 1px solid #ccc;
                margin: 0 5px;
            }}
            a {{
                font-size: 18px;
                color: #4a90e2;
                text-decoration: none;
            }}
            a:hover {{
                text-decoration: underline;
            }}
            .link-btn {{
                display: inline-block;
                background: #eef4fc;
                color: #357ABD;
                padding: 12px 22px;
                margin: 6px;
                border-radius: 10px;
                border: 1px solid #cfe0f5;
                font-size: 18px;
                text-decoration: none;
            }}
            .link-btn:hover {{
                background: #dbe9fb;
                text-decoration: none;
            }}
            .btn-sub {{
                display: block;
                font-size: 12px;
                color: #7a97b8;
                margin-top: 2px;
            }}
            button .btn-sub {{
                color: rgba(255,255,255,0.85);
            }}
            .clock {{
                font-size: 16px;
                color: #666;
                margin-bottom: 10px;
            }}
            .popup-overlay {{
                display: none;
                position: fixed;
                top: 0; left: 0; right: 0; bottom: 0;
                background: rgba(0,0,0,0.5);
                z-index: 1000;
            }}
            .popup-box {{
                background: white;
                padding: 30px;
                border-radius: 12px;
                width: 80%;
                max-width: 400px;
                margin: 8% auto;
                text-align: center;
                max-height: 80vh;
                overflow-y: auto;
            }}
        </style>
    </head>
    <body>
        {member_qr_html}
        <div class="clock">🕒 <span id="live-clock"></span></div>
        <h1>{title}</h1>
        <div class="card">
            {body}
            <br>
            <button onclick="{back_button_js}" style="background:#999;">戻る</button>
        </div>
        {round_watcher_html}
        <div style="margin-top:20px; font-size:11px; color:#aaa;">v{APP_VERSION}</div>
        <script>
            function updateClock() {{
                const now = new Date();
                const el = document.getElementById("live-clock");
                if (el) {{
                    el.innerText = now.toLocaleTimeString('ja-JP', {{ hour12: false }});
                }}
            }}
            updateClock();
            setInterval(updateClock, 1000);
            if ('serviceWorker' in navigator) {{
                navigator.serviceWorker.register('/sw.js').catch(() => {{}});
            }}
        </script>
    </body>
    </html>
    """)

# -----------------------
# MEMBER FLOW
# -----------------------

@app.get("/", response_class=HTMLResponse)
def welcome(registered: str = None):
    body = f"""
    <div style="text-align:center; margin-bottom:16px;">
        <img src="/qr/member" width="160" height="160" alt="Member QR" style="border-radius:8px; box-shadow:0 2px 6px rgba(0,0,0,0.15);" />
        <div style="display:flex; gap:8px; justify-content:center; margin-top:10px;">
            <button onclick="document.getElementById('qr-share-popup').style.display='block'" style="background:#eee; color:#666; font-size:13px; padding:6px 14px;">QRコードを共有<span class="btn-sub">Share QR</span></button>
            <button onclick="sharePageLink()" style="background:#eee; color:#666; font-size:13px; padding:6px 14px;">🔗 リンクを共有<span class="btn-sub">Share page link</span></button>
        </div>
    </div>
    <div class="popup-overlay" id="qr-share-popup">
        <div class="popup-box">
            <img src="/qr/member" width="220" height="220" alt="Member QR" style="border-radius:8px;" />
            <div style="margin-top:16px; display:flex; gap:10px; justify-content:center;">
                <button onclick="shareQrImage()">共有<span class="btn-sub">Share</span></button>
                <button onclick="document.getElementById('qr-share-popup').style.display='none'" style="background:#999;">閉じる<span class="btn-sub">Close</span></button>
            </div>
        </div>
    </div>
    <script>
        async function shareQrImage() {{
            const qrUrl = window.location.origin + '/qr/member';
            try {{
                const resp = await fetch('/qr/member');
                const blob = await resp.blob();
                const file = new File([blob], 'member-qr.png', {{ type: 'image/png' }});
                if (navigator.canShare && navigator.canShare({{ files: [file] }})) {{
                    await navigator.share({{ files: [file], title: 'スパー参加用QRコード' }});
                    return;
                }} else if (navigator.share) {{
                    await navigator.share({{ title: 'スパー参加用QRコード', url: qrUrl }});
                    return;
                }}
            }} catch (e) {{}}
            try {{
                await navigator.clipboard.writeText(qrUrl);
                alert('この端末では共有機能が使えないため、リンクをコピーしました。\\nCould not share directly, copied the link instead:\\n' + qrUrl);
            }} catch (e) {{
                alert('この端末では共有機能が使えません。リンク: \\nShare is unavailable on this device. Link:\\n' + qrUrl);
            }}
        }}
        async function sharePageLink() {{
            try {{
                if (navigator.share) {{
                    await navigator.share({{ title: document.title, url: window.location.href }});
                    return;
                }}
            }} catch (e) {{}}
            try {{
                await navigator.clipboard.writeText(window.location.href);
                alert('リンクをコピーしました\\nLink copied:\\n' + window.location.href);
            }} catch (e) {{
                alert('共有機能が使えません。リンク:\\nShare unavailable. Link:\\n' + window.location.href);
            }}
        }}
    </script>
    <div style="display:flex; justify-content:center; gap:50px; flex-wrap:wrap;">
        <div style="text-align:center;">
            <p style="font-weight:bold; color:#555; margin-bottom:8px;">Instructor</p>
            <a href="/admin"><img src="/static/instructor-photo.png" alt="Instructor" width="200" height="200"
                style="object-fit:cover; border-radius:12px; box-shadow:0 2px 8px rgba(0,0,0,0.15);" /></a>
        </div>
        <div style="text-align:center;">
            <p style="font-weight:bold; color:#555; margin-bottom:8px;">Member</p>
            <a href="/member/register"><img src="/static/member-icon.png" alt="Member" width="200" height="200"
                style="object-fit:contain; background:#f2f2f2; border-radius:12px; padding:20px; box-sizing:border-box; box-shadow:0 2px 8px rgba(0,0,0,0.15);" /></a>
        </div>
    </div>
    """
    if registered:
        body += f"""
        <div id="registered-toast" style="position:fixed; top:20px; left:50%; transform:translateX(-50%);
            background:#4a90e2; color:white; padding:15px 25px; border-radius:8px; font-size:18px;
            z-index:2000; box-shadow:0 4px 10px rgba(0,0,0,0.2);">
            ✓ {registered} さん、登録が完了しました！
        </div>
        <script>
            setTimeout(function() {{
                const t = document.getElementById('registered-toast');
                if (t) t.style.display = 'none';
                window.history.replaceState({{}}, '', '/');
            }}, 3000);
        </script>
        """
    return html_page("Welcome to the CAVE Expert Class", body)


@app.get("/qr/member")
def qr_member():
    return qr_image_response(f"{get_base_url()}/")


@app.get("/qr/instructor")
def qr_instructor():
    return qr_image_response(f"{get_base_url()}/admin")


def qr_image_response(data: str) -> Response:
    img = qrcode.make(data)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(content=buf.getvalue(), media_type="image/png")


@app.get("/member/register", response_class=HTMLResponse)
def member_register_page():
    body = """
    <a href="/member/register/join" class="link-btn">スパーリングに参加<span class="btn-sub">Join Sparring</span></a><br><br>
    <a href="/member/register/edit_time" class="link-btn">スパー参加時間を編集<span class="btn-sub">Edit Coming Time</span></a>
    """
    return html_page("スパー参加登録", body)


@app.get("/member/register/join", response_class=HTMLResponse)
def member_register_join_page():
    body = """
    <p>本日、定時に参加しますか？<span class="btn-sub" style="margin-top:4px;">Are you arriving on time today?</span></p>
    <form action="/member/register/attendance" method="post">
        <button name="on_time" value="yes" type="submit">はい<span class="btn-sub">Yes</span></button>
        <button name="on_time" value="no" type="submit">いいえ<span class="btn-sub">No</span></button>
    </form>
    """
    return html_page("スパーリング参加確認", body)


MEMBER_NAME_FORM_SCRIPT = """
<script>
    function submitMemberNameForm(event, form) {
        event.preventDefault();
        const data = new URLSearchParams(new FormData(form));
        fetch('/member/register/name', { method: 'POST', body: data })
            .then(function(r) { return r.json(); })
            .then(function(res) {
                if (res.status === 'duplicate') {
                    alert('この名前は既に登録されています。文字を追加して区別してください。\\nName already enrolled, add more letters to distinguish');
                } else {
                    window.location.href = res.redirect;
                }
            }).catch(function() {});
        return false;
    }
</script>
"""


def member_name_entry_form(on_time: str, late_time_a: str = None, late_time_b: str = None) -> str:
    if on_time == "yes":
        return f"""
        <form onsubmit="return submitMemberNameForm(event, this)">
            <input type="hidden" name="on_time" value="yes" />
            名前（重複不可）<span class="btn-sub" style="display:inline;">(Name, must be unique)</span>:
            <input type="text" name="name" required {NAME_VALIDATION_ATTRS} />
            <button type="submit">登録<span class="btn-sub">Register</span></button>
        </form>
        {MEMBER_NAME_FORM_SCRIPT}
        """
    now = now_jst()
    default_hour = int(late_time_a) if late_time_a else now.hour
    default_minute = int(late_time_b) if late_time_b else now.minute
    # 現在時刻より前の値が渡された場合は現在時刻まで繰り上げる
    if default_hour < now.hour or (default_hour == now.hour and default_minute < now.minute):
        default_hour, default_minute = now.hour, now.minute
    return f"""
    <form onsubmit="return submitMemberNameForm(event, this)">
        <input type="hidden" name="on_time" value="no" />
        何時に来ますか？<span class="btn-sub" style="display:inline;">What time will you arrive?</span><br>
        {time_picker("late_time", first_default=default_hour, second_default=default_minute,
                      min_hour=now.hour, min_minute=now.minute)}
        <br><br>
        名前（重複不可）<span class="btn-sub" style="display:inline;">(Name, must be unique)</span>:
        <input type="text" name="name" required {NAME_VALIDATION_ATTRS} />
        <button type="submit">登録<span class="btn-sub">Register</span></button>
    </form>
    {MEMBER_NAME_FORM_SCRIPT}
    """


@app.post("/member/register/attendance", response_class=HTMLResponse)
def member_register_attendance(on_time: str = Form(...)):
    body = member_name_entry_form(on_time)
    return html_page("スパー参加登録", body, back_url="/member/register")


@app.post("/member/register/name", response_class=HTMLResponse)
def member_register_name(
    name: str = Form(...),
    on_time: str = Form(...),
    late_time_a: str = Form(None),
    late_time_b: str = Form(None),
    db: Session = Depends(get_db)
):
    existing = db.query(Member).filter(Member.name == name).first()
    if existing:
        return JSONResponse({"status": "duplicate"})
    member = Member(name=name)
    db.add(member)
    db.commit()
    db.refresh(member)

    late_time = f"{late_time_a}:{late_time_b}" if on_time == "no" else None
    ts = TodaySparring(
        member_id=member.id,
        join_type="on_time" if on_time == "yes" else "late",
        late_time=late_time,
        date=today_jst(),
        joined_at=now_jst()
    )
    db.add(ts)
    db.commit()

    if on_time == "no":
        insert_late_joiner_priority(db, member.id, today_jst())

    return JSONResponse({"status": "ok", "redirect": f"/?registered={quote(member.name)}"})


# -----------------------
# EDIT COMING TIME (スパー参加時間)
# -----------------------

@app.get("/member/register/edit_time", response_class=HTMLResponse)
def member_edit_time_page(db: Session = Depends(get_db)):
    today = today_jst()
    members_today = db.query(TodaySparring).filter(TodaySparring.date == today).all()

    if not members_today:
        body = """
        <p>本日の参加者がいません。</p>
        """
        return html_page("スパー参加時間を編集", body)

    names_json = json.dumps([ts.member.name for ts in members_today])
    body = f"""
    <p>お名前を選択、または直接入力してください</p>
    <form action="/member/register/edit_time" method="post">
        <div style="position:relative; display:inline-block; width:60%;">
            <input type="text" id="name-input" name="name" required {NAME_VALIDATION_ATTRS}
                autocomplete="off" style="width:100%; box-sizing:border-box; margin:0;" />
            <div id="name-suggestions" style="display:none; position:absolute; top:100%; left:0; width:100%;
                box-sizing:border-box; background:white; border:1px solid #ccc; border-top:none;
                border-radius:0 0 8px 8px; max-height:200px; overflow-y:auto; z-index:100; text-align:left;"></div>
        </div>
        <br><br>
        <button type="submit">次へ</button>
    </form>
    <script>
        (function() {{
            const allNames = {names_json};
            const input = document.getElementById('name-input');
            const suggBox = document.getElementById('name-suggestions');
            input.addEventListener('input', function() {{
                const val = input.value.trim().toLowerCase();
                suggBox.innerHTML = '';
                if (!val) {{ suggBox.style.display = 'none'; return; }}
                const matches = allNames.filter(function(n) {{ return n.toLowerCase().includes(val); }}).slice(0, 8);
                if (matches.length === 0) {{ suggBox.style.display = 'none'; return; }}
                matches.forEach(function(n) {{
                    const item = document.createElement('div');
                    item.textContent = n;
                    item.style.padding = '10px 12px';
                    item.style.cursor = 'pointer';
                    item.style.fontSize = '16px';
                    item.onmouseover = function() {{ item.style.background = '#f0f0f0'; }};
                    item.onmouseout = function() {{ item.style.background = 'white'; }};
                    item.onclick = function() {{
                        input.value = n;
                        suggBox.style.display = 'none';
                    }};
                    suggBox.appendChild(item);
                }});
                suggBox.style.display = 'block';
            }});
            document.addEventListener('click', function(e) {{
                if (e.target !== input) suggBox.style.display = 'none';
            }});
        }})();
    </script>
    """
    return html_page("スパー参加時間を編集", body)


@app.post("/member/register/edit_time", response_class=HTMLResponse)
def member_edit_time_select(name: str = Form(...), db: Session = Depends(get_db)):
    today = today_jst()
    ts = db.query(TodaySparring).join(Member).filter(
        Member.name == name,
        TodaySparring.date == today
    ).first()
    if not ts:
        return html_page("エラー", f"""
        <p>「{name}」さんの本日の参加記録が見つかりません。</p>
        """)

    member_id = ts.member_id
    now = now_jst()
    h_default, m_default = now.hour, now.minute
    if ts.late_time and ":" in ts.late_time:
        h_str, m_str = ts.late_time.split(":")
        h_default, m_default = int(h_str), int(m_str)
        # 現在時刻より前の値が渡された場合は現在時刻まで繰り上げる
        if h_default < now.hour or (h_default == now.hour and m_default < now.minute):
            h_default, m_default = now.hour, now.minute

    body = f"""
    <p>{ts.member.name} さんのスパー参加時間を編集<span class="btn-sub" style="margin-top:4px;">Edit arrival time</span></p>
    <form id="edit-time-save-form" action="/member/register/edit_time/save" method="post">
        <input type="hidden" name="member_id" value="{member_id}" />
        何時に来ますか？<span class="btn-sub" style="display:inline;">What time will you arrive?</span><br>
        {time_picker("late_time", first_default=h_default, second_default=m_default,
                      min_hour=now.hour, min_minute=now.minute)}
    </form>
    <div style="display:flex; gap:10px; justify-content:center; margin-top:16px;">
        <button type="submit" form="edit-time-save-form">更新<span class="btn-sub">Update</span></button>
        <form action="/member/register/edit_time/cancel" method="post"
            onsubmit="return confirm('本日の参加を取り消しますか？')" style="margin:0;">
            <input type="hidden" name="member_id" value="{member_id}" />
            <button type="submit" style="background:#c0392b;">不参加 (今日は行けません)<span class="btn-sub">Not attending today</span></button>
        </form>
    </div>
    """
    return html_page("スパー参加時間を編集", body)


@app.post("/member/register/edit_time/save", response_class=HTMLResponse)
def member_edit_time_save(
    member_id: int = Form(...),
    late_time_a: str = Form(...),
    late_time_b: str = Form(...),
    db: Session = Depends(get_db)
):
    today = today_jst()
    ts = db.query(TodaySparring).filter(
        TodaySparring.member_id == member_id,
        TodaySparring.date == today
    ).first()
    if not ts:
        return html_page("エラー", """
        <p>本日の参加記録が見つかりません。</p>
        """)

    ts.join_type = "late"
    ts.late_time = f"{late_time_a}:{late_time_b}"
    db.commit()

    body = f"""
    <p>{ts.member.name} さんのスパー参加時間を更新しました。</p>
    <a href="/" class="link-btn">トップに戻る</a>
    """
    return html_page("更新完了", body)


@app.post("/member/register/edit_time/cancel", response_class=HTMLResponse)
def member_edit_time_cancel(member_id: int = Form(...), db: Session = Depends(get_db)):
    today = today_jst()
    ts = db.query(TodaySparring).filter(
        TodaySparring.member_id == member_id,
        TodaySparring.date == today
    ).first()
    if not ts:
        return html_page("エラー", """
        <p>本日の参加記録が見つかりません。</p>
        """)

    member_name = ts.member.name
    db.delete(ts)
    db.commit()
    resolve_pairs_after_absence(db, member_id, today)

    body = f"""
    <p>{member_name} さんの本日の参加を取り消しました。</p>
    <a href="/" class="link-btn">トップに戻る</a>
    """
    return html_page("取消完了", body)


@app.get("/member/today/{member_id}", response_class=HTMLResponse)
def member_today_group(member_id: int, db: Session = Depends(get_db)):
    member = db.query(Member).filter(Member.id == member_id).first()
    today = today_jst()
    groups = db.query(SparringGroup).filter(SparringGroup.date == today).all()

    my_groups = [
        g for g in groups
        if g.member_a_id == member.id or g.member_b_id == member.id or g.waiting_member_id == member.id
    ]

    body = f"<p>{member.name} さんの本日のスパーリング組み合わせ</p>"
    if not my_groups:
        body += "<p>まだ組み合わせが作成されていません。</p>"
    else:
        for g in my_groups:
            if g.is_waiting:
                body += f"<div class='wait-box'>待機: {member.name}</div>"
            else:
                a = db.query(Member).filter(Member.id == g.member_a_id).first()
                b = db.query(Member).filter(Member.id == g.member_b_id).first()
                body += f"<div class='pair-box'>ペア {g.pair_number}: {a.name} vs {b.name}</div>"

    return html_page("本日のスパーリング", body)

# -----------------------
# ADMIN FLOW（パスワード保護: "cave"）
# -----------------------

@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_page():
    body = """
    <form action="/admin/login" method="post">
        パスワード: <input type="password" name="password" autofocus />
        <button type="submit">ログイン</button>
    </form>
    """
    return html_page("インストラクターページ ログイン", body)


@app.post("/admin/login", response_class=HTMLResponse)
def admin_login(password: str = Form(...), db: Session = Depends(get_db)):
    if password.strip().lower() == ADMIN_PASSWORD:
        # ログイン後はその日のうちは再要求しない。日付が変われば再度必要になる
        response = RedirectResponse(url="/admin", status_code=303)
        response.set_cookie("admin_auth", str(today_jst()), httponly=True, samesite="lax", max_age=60 * 60 * 36)
        return response
    return html_page("インストラクターページ ログイン", """
    <p style="color:#c0392b;">パスワードが違います。</p>
    <form action="/admin/login" method="post">
        パスワード: <input type="password" name="password" autofocus />
        <button type="submit">ログイン</button>
    </form>
    """)


@app.get("/admin", response_class=HTMLResponse)
def admin_home(db: Session = Depends(get_db), _auth: None = Depends(require_admin)):
    today = today_jst()
    total_today = db.query(TodaySparring).filter(TodaySparring.date == today).count()
    has_existing_groups = db.query(SparringGroup).filter(SparringGroup.date == today).first() is not None

    if has_existing_groups:
        generate_link = (
            '<a class="link-btn" href="/admin/generate" '
            'onclick="return confirm(\'スパーリングを再生成すると、現在の進行状況がリセットされます。よろしいですか？\')">'
            'スパーリング開始 (Start the Sparring)</a>'
        )
    else:
        generate_link = '<a class="link-btn" href="/admin/generate">スパーリング開始 (Start the Sparring)</a>'

    body = f"""
    <p style="color:#666;">本日の参加者数: {total_today} 名</p>
    <a href="/admin/members" class="link-btn">本日の参加者確認 (Check up Sparring Member)</a><br><br>
    <a href="/admin/status" class="link-btn">スパーリング状況 (Sparring Status)</a><br><br>
    <a href="/admin/setup" class="link-btn">設定 (Set up)</a><br><br>
    {generate_link}
    """
    return html_page("インストラクターページ", body, back_url="/", admin_round_watcher=True)


def render_round_groups(db: Session, group_list) -> str:
    html = ""
    for g in group_list:
        if not g.is_waiting:
            a = db.query(Member).filter(Member.id == g.member_a_id).first()
            b = db.query(Member).filter(Member.id == g.member_b_id).first()
            html += f"<div class='pair-box'>ペア {g.pair_number}: {a.name} vs {b.name}</div>"
    return html


def render_round_chips(db: Session, group_list) -> str:
    html = ""
    for g in group_list:
        if not g.is_waiting:
            a = db.query(Member).filter(Member.id == g.member_a_id).first()
            b = db.query(Member).filter(Member.id == g.member_b_id).first()
            html += f"<div class='pair-chip'>ペア{g.pair_number}<br>{a.name} vs {b.name}</div>"
    return html


def render_participant_cell(member_id: int, name: str, join_type: str, late_time: str, joined_at_label: str) -> str:
    text = f"{name} - {'定時' if join_type == 'on_time' else '遅刻'}"
    if join_type == "late":
        text += f"（到着予定: {late_time}）"
    if joined_at_label:
        text += f" <span style='color:#999;font-size:13px;'>[{joined_at_label}]</span>"
    return f"""
    <div style='padding:6px; border-bottom:1px solid #eee;'>
        {text}<br>
        <form action="/admin/members/remove" method="post" onsubmit="return confirm('本日の参加者から削除しますか？')" style="display:inline;">
            <input type="hidden" name="member_id" value="{member_id}" />
            <button type="submit" style="background:#c0392b; padding:4px 10px; font-size:12px; margin:4px 0 0;">不参加</button>
        </form>
    </div>
    """


@app.get("/admin/members", response_class=HTMLResponse)
def admin_members_page(db: Session = Depends(get_db), _auth: None = Depends(require_admin)):
    today = today_jst()
    members_today = db.query(TodaySparring).filter(TodaySparring.date == today).all()

    grid_html = "".join(
        render_participant_cell(
            ts.member_id, ts.member.name, ts.join_type, ts.late_time,
            ts.joined_at.strftime("%H:%M") if ts.joined_at else ""
        )
        for ts in members_today
    )
    body = f"""
    <h2 id="participant-count">{len(members_today):02d}名</h2>
    <form id="add-form" onsubmit="return submitAddParticipant(event, this)" style="margin-bottom:16px;">
        名前: <input type="text" name="name" required {NAME_VALIDATION_ATTRS} />
        <button type="submit">追加</button>
    </form>

    <div id="participant-list" style="display:grid; grid-template-columns: repeat(4, 1fr); gap:4px; text-align:left; font-size:14px;">
        {grid_html}
    </div>

    <script>
        function submitAddParticipant(event, form) {{
            event.preventDefault();
            const input = form.querySelector('input[name="name"]');
            const name = input.value.trim();
            if (!name) return false;
            fetch('/admin/members/add', {{
                method: 'POST',
                headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                body: 'name=' + encodeURIComponent(name)
            }}).then(function(r) {{ return r.json(); }}).then(function(data) {{
                if (data.status === 'duplicate') {{
                    alert('この名前は既に登録されています。文字を追加して区別してください。\\nName already enrolled, add more letters to distinguish');
                }} else {{
                    input.value = '';
                    if (typeof refreshParticipants === 'function') refreshParticipants();
                }}
            }}).catch(function() {{}});
            return false;
        }}
        (function() {{
            function escapeHtml(s) {{
                return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
            }}
            function refreshParticipants() {{
                fetch('/admin/members/poll').then(function(r) {{ return r.json(); }}).then(function(data) {{
                    const countEl = document.getElementById('participant-count');
                    if (countEl) countEl.innerText = String(data.count).padStart(2, '0') + '名';
                    const list = document.getElementById('participant-list');
                    if (data.members.length === 0) {{
                        list.innerHTML = '';
                        return;
                    }}
                    list.innerHTML = data.members.map(function(m) {{
                        let text = escapeHtml(m.name) + ' - ' + (m.join_type === 'on_time' ? '定時' : '遅刻');
                        if (m.join_type !== 'on_time') text += '（到着予定: ' + escapeHtml(m.late_time || '') + '）';
                        if (m.joined_at) text += ' <span style="color:#999;font-size:13px;">[' + m.joined_at + ']</span>';
                        return '<div style="padding:6px; border-bottom:1px solid #eee;">' + text +
                            '<br><form action="/admin/members/remove" method="post" onsubmit="return confirm(\\'本日の参加者から削除しますか？\\')" style="display:inline;">' +
                            '<input type="hidden" name="member_id" value="' + m.member_id + '" />' +
                            '<button type="submit" style="background:#c0392b; padding:4px 10px; font-size:12px; margin:4px 0 0;">不参加</button>' +
                            '</form></div>';
                    }}).join('');
                }}).catch(function() {{}});
            }}
            window.refreshParticipants = refreshParticipants;
            setInterval(refreshParticipants, 4000);
        }})();
    </script>
    """
    return html_page("本日の参加者確認", body, back_url="/admin", admin_round_watcher=True)


@app.get("/admin/members/poll")
def admin_members_poll(db: Session = Depends(get_db), _auth: None = Depends(require_admin)):
    today = today_jst()
    members_today = db.query(TodaySparring).filter(TodaySparring.date == today).all()
    return JSONResponse({
        "count": len(members_today),
        "members": [
            {
                "member_id": ts.member_id,
                "name": ts.member.name,
                "join_type": ts.join_type,
                "late_time": ts.late_time,
                "joined_at": ts.joined_at.strftime("%H:%M") if ts.joined_at else None
            }
            for ts in members_today
        ]
    })


@app.post("/admin/members/remove", response_class=HTMLResponse)
def admin_members_remove(member_id: int = Form(...), db: Session = Depends(get_db), _auth: None = Depends(require_admin)):
    today = today_jst()
    ts = db.query(TodaySparring).filter(
        TodaySparring.member_id == member_id,
        TodaySparring.date == today
    ).first()
    if ts:
        db.delete(ts)
        db.commit()
        resolve_pairs_after_absence(db, member_id, today)
    return RedirectResponse(url="/admin/members", status_code=303)


@app.post("/admin/members/add")
def admin_members_add(
    name: str = Form(...),
    db: Session = Depends(get_db),
    _auth: None = Depends(require_admin)
):
    today = today_jst()
    member = db.query(Member).filter(Member.name == name).first()
    if member:
        existing_ts = db.query(TodaySparring).filter(
            TodaySparring.member_id == member.id,
            TodaySparring.date == today
        ).first()
        if existing_ts:
            return JSONResponse({"status": "duplicate"})
    else:
        member = Member(name=name)
        db.add(member)
        db.commit()
        db.refresh(member)

    ts = TodaySparring(member_id=member.id, date=today, joined_at=now_jst())
    db.add(ts)
    ts.join_type = "on_time"
    ts.late_time = None
    db.commit()

    # 途中参加者は、画面外のラウンドに優先的に組み込む
    insert_late_joiner_priority(db, member.id, today)

    count = db.query(TodaySparring).filter(TodaySparring.date == today).count()
    return JSONResponse({"status": "ok", "count": count})


@app.get("/admin/setup", response_class=HTMLResponse)
def admin_setup_page(db: Session = Depends(get_db), _auth: None = Depends(require_admin)):
    setting = db.query(SparringSettings).order_by(SparringSettings.id.desc()).first()

    if setting:
        pairs_value = setting.number_of_pairs
        rounds_value = setting.number_of_rounds
        skip_value = setting.max_skip_rounds
        duration_m, duration_s = map(int, setting.round_duration.split(":"))
        break_m, break_s = map(int, (setting.break_duration or "00:30").split(":"))
    else:
        pairs_value, rounds_value, skip_value = 4, 3, 2
        duration_m, duration_s = 3, 0
        break_m, break_s = 0, 30

    # 現在の参加者数とペア数から、数学的に可能な最短の待機ラウンド数を計算して表示する
    today = today_jst()
    total_participants = db.query(TodaySparring).filter(TodaySparring.date == today).count()
    play_slots = pairs_value * 2
    if play_slots > 0 and total_participants > play_slots:
        min_skip = -(-(total_participants - play_slots) // play_slots)
    else:
        min_skip = 0

    body = f"""
    <form action="/admin/setup" method="post">
        ペア数 (Number of Pairs): <input type="number" name="number_of_pairs" id="pairs_input" value="{pairs_value}" oninput="updateMinSkipHint()" /><br><br>
        <div style="display:flex; gap:30px; flex-wrap:wrap;">
            <div>
                ラウンド時間 (Round duration):<br>
                {time_picker("round_duration", first_default=duration_m, second_default=duration_s, first_max=10, first_label="分", second_label="秒")}
            </div>
            <div>
                休憩時間 (Break between rounds):<br>
                {time_picker("break_duration", first_default=break_m, second_default=break_s, first_max=10, first_label="分", second_label="秒")}
            </div>
        </div>
        <p style="font-size:14px;color:#666;">※ ラウンド終了後、この時間操作がなければ自動で次のラウンドに進みます。</p>
        <br>
        ラウンド数 (Number of Sparrings): <input type="number" name="number_of_rounds" value="{rounds_value}" /><br><br>
        <div style="text-align:center;">
            スパーリングスキップ (最大連続待機ラウンド数):<br>
            <select name="max_skip_rounds" id="skip_input" style="margin-top:6px;"></select>
            <p id="min-skip-hint" class="btn-sub" style="margin-top:4px;"></p>
        </div>
        <p style="font-size:14px;color:#666;">※ この回数を超えて連続で待機させないよう、優先的に組み合わせます。</p>
        <button type="submit">保存</button>
    </form>
    <script>
        const totalParticipants = {total_participants};
        const savedSkipValue = {skip_value};
        function updateMinSkipHint() {{
            const pairs = parseInt(document.getElementById('pairs_input').value, 10) || 0;
            const slots = pairs * 2;
            let minSkip = 0;
            if (slots > 0 && totalParticipants > slots) {{
                minSkip = Math.ceil((totalParticipants - slots) / slots);
            }}
            const hintEl = document.getElementById('min-skip-hint');
            if (hintEl) {{
                hintEl.innerText = '現在' + totalParticipants + '名・' + pairs + 'ペアでの理論上の最短待機: ' + minSkip;
            }}
            // 理論上の最短待機を下回る値は選べないようにする（ドロップダウンで固定）
            const selectEl = document.getElementById('skip_input');
            if (selectEl) {{
                const current = selectEl.value ? parseInt(selectEl.value, 10) : savedSkipValue;
                const keep = Math.max(current, minSkip);
                let html = '';
                for (let v = minSkip; v <= minSkip + 10; v++) {{
                    html += '<option value="' + v + '"' + (v === keep ? ' selected' : '') + '>' + v + '</option>';
                }}
                selectEl.innerHTML = html;
            }}
        }}
        updateMinSkipHint();
    </script>
    """
    return html_page("設定ページ", body, back_url="/admin", admin_round_watcher=True)


@app.post("/admin/setup", response_class=HTMLResponse)
def admin_setup(
    number_of_pairs: int = Form(...),
    round_duration_a: str = Form(...),
    round_duration_b: str = Form(...),
    number_of_rounds: int = Form(...),
    max_skip_rounds: int = Form(...),
    break_duration_a: str = Form("00"),
    break_duration_b: str = Form("30"),
    db: Session = Depends(get_db),
    _auth: None = Depends(require_admin)
):
    setting = SparringSettings(
        number_of_pairs=number_of_pairs,
        round_duration=f"{round_duration_a}:{round_duration_b}",
        number_of_rounds=number_of_rounds,
        max_skip_rounds=max_skip_rounds,
        break_duration=f"{break_duration_a}:{break_duration_b}"
    )
    db.add(setting)
    db.commit()
    return RedirectResponse(url="/admin", status_code=303)


def resolve_pairs_after_absence(db: Session, absent_member_id: int, today: date):
    # 「表示中のグループを固定する」とは、影響を受けていないペアを勝手に
    # 組み直さないという意味。ステータスが変わった本人が含まれるペアだけを、
    # それが今のラウンドでも先のラウンドでも直接編集する。
    active_ids = set(
        mid for (mid,) in db.query(TodaySparring.member_id).filter(TodaySparring.date == today).all()
    )

    affected_pairs = db.query(SparringGroup).filter(
        SparringGroup.date == today,
        SparringGroup.is_waiting == False,
        or_(
            SparringGroup.member_a_id == absent_member_id,
            SparringGroup.member_b_id == absent_member_id
        )
    ).all()

    for pair in affected_pairs:
        round_num = pair.round_number
        a_gone = pair.member_a_id not in active_ids
        b_gone = pair.member_b_id not in active_ids
        needed = (1 if a_gone else 0) + (1 if b_gone else 0)
        if needed == 0:
            continue

        waiting_rows = db.query(SparringGroup).filter(
            SparringGroup.date == today,
            SparringGroup.round_number == round_num,
            SparringGroup.is_waiting == True,
            SparringGroup.waiting_member_id.in_(active_ids)
        ).limit(needed).all()

        if len(waiting_rows) < needed:
            # 補充できる待機者が足りない場合はそのペアを解消する
            if needed == 1:
                remaining_id = pair.member_b_id if a_gone else pair.member_a_id
                db.add(SparringGroup(
                    round_number=round_num, pair_number=0,
                    waiting_member_id=remaining_id, is_waiting=True,
                    remaining_time=pair.remaining_time, date=today
                ))
            db.delete(pair)
            continue

        for w in waiting_rows:
            new_id = w.waiting_member_id
            db.delete(w)
            if a_gone:
                pair.member_a_id = new_id
                a_gone = False
            else:
                pair.member_b_id = new_id
                b_gone = False

    db.commit()


def parse_duration_seconds(duration_str: str) -> int:
    mm, ss = map(int, duration_str.split(":"))
    return mm * 60 + ss


def member_arrival_dt(join_type: str, late_time: str, today: date):
    # 定時参加なら常に対戦可能。遅刻の場合は到着予定時刻より前のラウンドには入れない。
    if join_type != "late" or not late_time or ":" not in late_time:
        return None
    h, m = map(int, late_time.split(":"))
    return datetime.combine(today, datetime.min.time()).replace(hour=h, minute=m)


def generate_round_range(db: Session, today: date, setting: SparringSettings,
                          member_ids: list, arrival_dt: dict, waiting_streak: dict,
                          paired_history: set, base_time: datetime, from_round: int, to_round: int):
    duration_seconds = parse_duration_seconds(setting.round_duration)
    play_slots = setting.number_of_pairs * 2

    for round_num in range(from_round, to_round + 1):
        round_start = base_time + timedelta(seconds=(round_num - from_round) * duration_seconds)
        # まだ到着予定時刻前の遅刻者は、そのラウンドの組み合わせに一切登場させない
        eligible = [
            mid for mid in member_ids
            if arrival_dt.get(mid) is None or arrival_dt[mid] <= round_start
        ]
        # 待機が長い人ほど優先的に対戦させる（同点は元の並び順で安定ソート）
        order = sorted(eligible, key=lambda mid: -waiting_streak[mid])
        playing_pool = order[:play_slots]
        waiting = order[play_slots:]

        # 優先順位を保ちつつ、なるべく初対戦の相手を選んで同じペアの繰り返しを避ける
        remaining = list(playing_pool)
        pair_num = 1
        while len(remaining) >= 2:
            a = remaining.pop(0)
            partner_idx = 0
            for i, b in enumerate(remaining):
                if frozenset((a, b)) not in paired_history:
                    partner_idx = i
                    break
            b = remaining.pop(partner_idx)
            paired_history.add(frozenset((a, b)))
            db.add(SparringGroup(
                round_number=round_num, pair_number=pair_num,
                member_a_id=a, member_b_id=b, is_waiting=False,
                remaining_time=setting.round_duration, date=today
            ))
            waiting_streak[a] = 0
            waiting_streak[b] = 0
            pair_num += 1

        # ペアを組めなかった余り1人がいれば待機扱い
        waiting = remaining + waiting
        for mid in waiting:
            waiting_streak[mid] += 1
            db.add(SparringGroup(
                round_number=round_num, pair_number=0,
                waiting_member_id=mid, is_waiting=True,
                remaining_time=setting.round_duration, date=today
            ))


def insert_late_joiner_priority(db: Session, member_id: int, today: date):
    # まだ組み合わせが生成されていなければ、次回の生成で自然に組み込まれる
    if not db.query(SparringGroup).filter(SparringGroup.date == today).first():
        return

    setting = db.query(SparringSettings).order_by(SparringSettings.id.desc()).first()
    if not setting:
        return

    state = db.query(SparringState).filter(SparringState.date == today).first()
    current_round = state.current_round if state else 1
    # 画面に表示中の4ラウンド（現在＋次の3）はそのまま固定し、それより先だけ組み直す
    locked_until = current_round + 3
    from_round = locked_until + 1
    if from_round > setting.number_of_rounds:
        return

    participants = db.query(TodaySparring).filter(TodaySparring.date == today).all()
    member_ids_sorted = sorted(set(p.member_id for p in participants))
    arrival_dt = {p.member_id: member_arrival_dt(p.join_type, p.late_time, today) for p in participants}

    # これまでの対戦履歴は、これから消す画面外の分も含めて再利用する
    paired_history = set()
    for g in db.query(SparringGroup).filter(SparringGroup.date == today, SparringGroup.is_waiting == False).all():
        if g.member_a_id and g.member_b_id:
            paired_history.add(frozenset((g.member_a_id, g.member_b_id)))

    db.query(SparringGroup).filter(
        SparringGroup.date == today,
        SparringGroup.round_number >= from_round
    ).delete()

    # 新しく来た人を最優先にし、それ以外は待機カウントをリセットして組み直す
    waiting_streak = {mid: (10 ** 6 if mid == member_id else 0) for mid in member_ids_sorted}
    duration_seconds = parse_duration_seconds(setting.round_duration)
    base_time = now_jst() + timedelta(seconds=duration_seconds)

    generate_round_range(
        db, today, setting, member_ids_sorted, arrival_dt, waiting_streak,
        paired_history, base_time=base_time, from_round=from_round, to_round=setting.number_of_rounds
    )
    db.commit()


@app.get("/admin/generate", response_class=HTMLResponse)
def admin_generate_groups(db: Session = Depends(get_db), _auth: None = Depends(require_admin)):
    today = today_jst()
    setting = db.query(SparringSettings).order_by(SparringSettings.id.desc()).first()
    if not setting:
        # 設定が未入力でもデフォルト値でスパーリングを開始できるようにする
        setting = SparringSettings(
            number_of_pairs=4,
            round_duration="03:00",
            number_of_rounds=3,
            max_skip_rounds=2
        )
        db.add(setting)
        db.commit()
        db.refresh(setting)

    participants = db.query(TodaySparring).filter(TodaySparring.date == today).all()
    if not participants:
        return html_page("エラー", "本日の参加者がいません。", back_url="/admin")

    member_ids_sorted = sorted(set(p.member_id for p in participants))
    arrival_dt = {p.member_id: member_arrival_dt(p.join_type, p.late_time, today) for p in participants}

    db.query(SparringGroup).filter(SparringGroup.date == today).delete()
    db.commit()

    waiting_streak = {mid: 0 for mid in member_ids_sorted}
    paired_history = set()
    generate_round_range(
        db, today, setting, member_ids_sorted, arrival_dt, waiting_streak,
        paired_history, base_time=now_jst(), from_round=1, to_round=setting.number_of_rounds
    )
    db.commit()

    # 本日のラウンド進行状態をリセット
    db.query(SparringState).filter(SparringState.date == today).delete()
    db.add(SparringState(date=today, current_round=1, round_started_at=None, is_running=False))
    db.commit()

    return RedirectResponse(url="/admin/status", status_code=303)

@app.get("/admin/status", response_class=HTMLResponse)
def admin_status(db: Session = Depends(get_db), _auth: None = Depends(require_admin)):
    today = today_jst()
    groups = db.query(SparringGroup).filter(SparringGroup.date == today).all()
    setting = db.query(SparringSettings).order_by(SparringSettings.id.desc()).first()

    if not groups:
        body = "<h2>スパーリング状況</h2><p>まだ組み合わせがありません。</p>"
        return html_page("スパーリング状況", body, back_url="/admin", member_qr_size=180)

    total_rounds = setting.number_of_rounds if setting else 0
    state = get_or_create_state(db, today)

    info_html = "<h2>スパーリング状況</h2>"
    if setting:
        mm, ss = map(int, setting.round_duration.split(":"))
        round_duration_seconds = mm * 60 + ss
        per_round = timedelta(minutes=mm, seconds=ss)
        remaining_rounds = max(total_rounds - state.current_round + 1, 0)
        info_html += f"<p>残りラウンド数: {remaining_rounds} ラウンド</p>"
        if remaining_rounds > 0:
            session_complete_time = now_jst() + per_round * remaining_rounds
            info_html += f"<p>セッション終了予定時刻: {session_complete_time.strftime('%H:%M')}</p>"
        bm, bs = map(int, (setting.break_duration or "00:30").split(":"))
        break_duration_seconds = bm * 60 + bs
    else:
        round_duration_seconds = 0
        break_duration_seconds = 30

    # 現在のラウンド
    popups_html = ""
    if state.current_round > total_rounds:
        left_html = "<h3>本日のスパーリングは終了しました！</h3>"
    else:
        current_groups = [g for g in groups if g.round_number == state.current_round]
        left_html = f"<h3>ラウンド {state.current_round}</h3>"

        if not state.is_running:
            # 初回ラウンドのみ「Ready to go?」の確認が必要（2ラウンド目以降は自動開始）
            settings_summary = (
                f"ラウンド時間: {setting.round_duration}　"
                f"ラウンド数: {setting.number_of_rounds}　"
                f"ペア数: {setting.number_of_pairs}"
                if setting else ""
            )
            left_html += f"""
            <p style="font-size:14px;color:#666;">{settings_summary}</p>
            <form action="/admin/status/start_round" method="post">
                <button type="submit">Start</button>
            </form>
            """
            left_html += render_round_groups(db, current_groups)

        elif state.is_paused:
            # ポップアップは使わず、一時停止ボタン自体を再開ボタンに切り替える
            # （サーバー側の状態がそのまま表示されるため、他のページへ移動して戻っても
            # 一時停止状態が正しく維持される）
            paused_m = (state.paused_remaining_seconds or 0) // 60
            paused_s = (state.paused_remaining_seconds or 0) % 60
            left_html += f"""
            <p>一時停止中 - 残り時間: <span style='font-size:28px;font-weight:bold;'>{paused_m:02d}:{paused_s:02d}</span></p>
            <div style="display:flex; gap:8px;">
                <form action="/admin/status/resume_round" method="post" style="margin:0;">
                    <button type="submit" style="background:#4a90e2;">再開する (Resume)</button>
                </form>
                <button type="button" onclick="handleSkipRound(true)" style="background:#999;">ラウンドスキップ<span class="btn-sub">Skip round</span></button>
            </div>
            <script>
                function handleSkipRound(alreadyPaused) {{
                    if (confirm('ラウンドをスキップしますか？ / Skip this round?')) {{
                        fetch('/admin/status/skip_round', {{ method: 'POST' }}).then(function() {{ window.location.reload(); }});
                    }} else if (!alreadyPaused) {{
                        fetch('/admin/status/resume_round', {{ method: 'POST' }}).then(function() {{ window.location.reload(); }});
                    }}
                }}
            </script>
            """
            left_html += render_round_groups(db, current_groups)

        else:
            left_html += """
            <p>Remaining TIME: <span id='timer' style='font-size:28px;font-weight:bold;'>--:--</span></p>
            <div style="display:flex; gap:8px;">
                <form action="/admin/status/pause_round" method="post" style="margin:0;">
                    <button type="submit" style="background:#e2954a;">一時停止 (Stop)</button>
                </form>
                <button type="button" onclick="handleSkipRound(false)" style="background:#999;">ラウンドスキップ<span class="btn-sub">Skip round</span></button>
            </div>
            <script>
                function handleSkipRound(alreadyPaused) {
                    function confirmAndSkip() {
                        if (confirm('ラウンドをスキップしますか？ / Skip this round?')) {
                            fetch('/admin/status/skip_round', { method: 'POST' }).then(function() { window.location.reload(); });
                        } else {
                            fetch('/admin/status/resume_round', { method: 'POST' }).then(function() { window.location.reload(); });
                        }
                    }
                    if (alreadyPaused) {
                        confirmAndSkip();
                    } else {
                        fetch('/admin/status/pause_round', { method: 'POST' }).then(confirmAndSkip);
                    }
                }
            </script>
            """
            left_html += render_round_groups(db, current_groups)

            start_iso = state.round_started_at.isoformat() + "+09:00"
            next_round_num = state.current_round + 1

            next_round_groups = [g for g in groups if g.round_number == next_round_num]
            next_pairs_html = ""
            if next_round_groups:
                next_pairs_html = f"<p>ラウンド{next_round_num}の組み合わせ:</p>"
                for g in next_round_groups:
                    if not g.is_waiting:
                        a = db.query(Member).filter(Member.id == g.member_a_id).first()
                        b = db.query(Member).filter(Member.id == g.member_b_id).first()
                        next_pairs_html += f"<div class='pair-box'>ペア {g.pair_number}: {a.name} vs {b.name}</div>"

            popups_html += f"""
            <div class="popup-overlay" id="round-complete-popup">
                <div class="popup-box">
                    <p>ラウンド{state.current_round}終了！</p>
                    {next_pairs_html}
                    <p>次のラウンドに進みますか？<span class="btn-sub" style="display:block;">
                        <span id="break-countdown">{break_duration_seconds}</span>秒操作がなければ自動で進みます</span></p>
                    <div style="display:flex; gap:10px; justify-content:center;">
                        <form action="/admin/status/next_round" method="post" style="margin:0;">
                            <button type="submit">Yes</button>
                        </form>
                        <form action="/admin/status/pause_round" method="post" style="margin:0;">
                            <button type="submit" style="background:#999;">No</button>
                        </form>
                    </div>
                </div>
            </div>
            <script>
                const startTime = new Date("{start_iso}");
                const duration = {round_duration_seconds};
                const breakDuration = {break_duration_seconds};
                let timerInterval;
                let breakInterval;
                function startBreakCountdown() {{
                    let remaining = breakDuration;
                    const el = document.getElementById('break-countdown');
                    breakInterval = setInterval(function() {{
                        remaining -= 1;
                        if (el) el.innerText = Math.max(remaining, 0);
                        if (remaining <= 0) {{
                            clearInterval(breakInterval);
                            fetch('/admin/status/next_round', {{ method: 'POST' }}).then(function() {{
                                window.location.reload();
                            }}).catch(function() {{}});
                        }}
                    }}, 1000);
                }}
                function tickTimer() {{
                    const elapsed = Math.floor((Date.now() - startTime.getTime()) / 1000);
                    const remaining = duration - elapsed;
                    const timerEl = document.getElementById('timer');
                    if (remaining <= 0) {{
                        timerEl.innerText = "00:00";
                        if (timerInterval) clearInterval(timerInterval);
                        document.getElementById('round-complete-popup').style.display = 'block';
                        startBreakCountdown();
                    }} else {{
                        const m = String(Math.floor(remaining / 60)).padStart(2, '0');
                        const s = String(remaining % 60).padStart(2, '0');
                        timerEl.innerText = m + ":" + s;
                    }}
                }}
                tickTimer();
                timerInterval = setInterval(tickTimer, 1000);
            </script>
            """

    # 次に控えている最大3ラウンド分を右側にコンパクト表示する
    upcoming_html = ""
    if state.current_round <= total_rounds:
        rounds_by_num = {}
        for g in groups:
            rounds_by_num.setdefault(g.round_number, []).append(g)
        upcoming_nums = [
            n for n in sorted(rounds_by_num.keys())
            if n > state.current_round
        ][:3]
        for r_num in upcoming_nums:
            chips = render_round_chips(db, rounds_by_num[r_num])
            upcoming_html += f"""
            <div class="round-overview">
                <h4>ラウンド {r_num}</h4>
                <div class="pair-chip-row">{chips}</div>
            </div>
            """

    share_link_script = """
    <script>
        async function sharePageLink() {
            try {
                if (navigator.share) {
                    await navigator.share({ title: document.title, url: window.location.href });
                    return;
                }
            } catch (e) {}
            try {
                await navigator.clipboard.writeText(window.location.href);
                alert('リンクをコピーしました\\nLink copied:\\n' + window.location.href);
            } catch (e) {
                alert('共有機能が使えません。リンク:\\nShare unavailable. Link:\\n' + window.location.href);
            }
        }
        function toggleStatusQr() {
            const box = document.getElementById('status-qr-box');
            if (!box) return;
            box.style.display = (box.style.display === 'none') ? '' : 'none';
        }
    </script>
    """

    body = f"""
    <div id="status-qr-box" style="text-align:center; margin-bottom:10px;">
        <img src="/qr/member" width="120" height="120" alt="Member QR" style="border-radius:8px; box-shadow:0 2px 6px rgba(0,0,0,0.15);" />
    </div>
    <div style="display:flex; gap:8px; flex-wrap:wrap; justify-content:center; margin-bottom:8px;">
        <button onclick="toggleStatusQr()" style="background:#eee; color:#666; font-size:13px; padding:6px 14px;">QRコード表示切替<span class="btn-sub">Toggle QR</span></button>
        <button onclick="sharePageLink()" style="background:#eee; color:#666; font-size:13px; padding:6px 14px;">🔗 リンクを共有<span class="btn-sub">Share page link</span></button>
    </div>
    {share_link_script}
    {info_html}
    <div style="display:flex; gap:16px; flex-wrap:wrap; align-items:flex-start; text-align:left;">
        <div style="flex:1 1 300px; background:#dceeff; border-radius:12px; padding:16px;">
            {left_html}
        </div>
        <div style="flex:1 1 300px;">
            <div style="padding:16px; background:#f2f2f2; border-radius:12px; text-align:left; margin-bottom:20px;">
                <h4 style="margin:0 0 10px;">参加者を直接追加<span class="btn-sub" style="display:inline;">Add participant directly</span></h4>
                <form onsubmit="return submitAddParticipantStatus(event, this)">
                    名前: <input type="text" name="name" required {NAME_VALIDATION_ATTRS} />
                    <button type="submit">追加</button>
                </form>
                <script>
                    const statusPairsValue = {setting.number_of_pairs if setting else 0};
                    function submitAddParticipantStatus(event, form) {{
                        event.preventDefault();
                        const input = form.querySelector('input[name="name"]');
                        const name = input.value.trim();
                        if (!name) return false;
                        fetch('/admin/members/add', {{
                            method: 'POST',
                            headers: {{'Content-Type': 'application/x-www-form-urlencoded'}},
                            body: 'name=' + encodeURIComponent(name)
                        }}).then(function(r) {{ return r.json(); }}).then(function(data) {{
                            if (data.status === 'duplicate') {{
                                alert('この名前は既に登録されています。文字を追加して区別してください。\\nName already enrolled, add more letters to distinguish');
                            }} else {{
                                input.value = '';
                                showMinSkipToast(data.count);
                            }}
                        }}).catch(function() {{}});
                        return false;
                    }}
                    function showMinSkipToast(count) {{
                        const slots = statusPairsValue * 2;
                        if (slots <= 0 || count <= slots) return;
                        const minSkip = Math.ceil((count - slots) / slots);
                        const toast = document.createElement('div');
                        toast.style.cssText = 'position:fixed; bottom:20px; left:50%; transform:translateX(-50%); background:#333; color:white; padding:10px 18px; border-radius:8px; font-size:14px; z-index:3000;';
                        toast.innerText = '理論上の最短待機ラウンド数: ' + minSkip;
                        document.body.appendChild(toast);
                        setTimeout(function() {{ toast.remove(); }}, 5000);
                    }}
                </script>
            </div>
            {upcoming_html}
        </div>
    </div>
    {popups_html}
    """

    return html_page("スパーリング状況", body, back_url="/admin", member_qr_size=180, wide=True,
                      admin_round_watcher=True, initial_round=state.current_round, initial_paused=state.is_paused,
                      initial_running=state.is_running)


@app.get("/admin/status/poll")
def admin_status_poll(db: Session = Depends(get_db), _auth: None = Depends(require_admin)):
    today = today_jst()
    groups = db.query(SparringGroup).filter(SparringGroup.date == today).all()
    setting = db.query(SparringSettings).order_by(SparringSettings.id.desc()).first()

    if not groups or not setting:
        return JSONResponse({"has_session": False})

    state = get_or_create_state(db, today)
    total_rounds = setting.number_of_rounds
    if state.current_round > total_rounds:
        return JSONResponse({"has_session": False})

    mm, ss = map(int, setting.round_duration.split(":"))
    round_duration_seconds = mm * 60 + ss
    bm, bs = map(int, (setting.break_duration or "00:30").split(":"))
    break_duration_seconds = bm * 60 + bs

    next_round_num = state.current_round + 1
    next_round_groups = [g for g in groups if g.round_number == next_round_num]
    next_pairs_html = ""
    if next_round_groups:
        next_pairs_html = f"<p>ラウンド{next_round_num}の組み合わせ:</p>"
        for g in next_round_groups:
            if not g.is_waiting:
                a = db.query(Member).filter(Member.id == g.member_a_id).first()
                b = db.query(Member).filter(Member.id == g.member_b_id).first()
                next_pairs_html += f"<div class='pair-box'>ペア {g.pair_number}: {a.name} vs {b.name}</div>"

    return JSONResponse({
        "has_session": True,
        "current_round": state.current_round,
        "total_rounds": total_rounds,
        "is_running": state.is_running,
        "is_paused": state.is_paused,
        "round_started_at": (state.round_started_at.isoformat() + "+09:00") if state.round_started_at else None,
        "round_duration_seconds": round_duration_seconds,
        "break_duration_seconds": break_duration_seconds,
        "next_pairs_html": next_pairs_html
    })


@app.post("/admin/status/start_round", response_class=HTMLResponse)
def admin_status_start_round(db: Session = Depends(get_db), _auth: None = Depends(require_admin)):
    today = today_jst()
    state = get_or_create_state(db, today)
    state.is_running = True
    state.round_started_at = now_jst()
    db.commit()
    return RedirectResponse(url="/admin/status", status_code=303)


@app.post("/admin/status/next_round", response_class=HTMLResponse)
def admin_status_next_round(db: Session = Depends(get_db), _auth: None = Depends(require_admin)):
    today = today_jst()
    state = get_or_create_state(db, today)
    state.current_round += 1
    # 初回ラウンドのみ「Ready to go?」で開始確認し、2ラウンド目以降は自動的に開始する
    state.is_running = True
    state.round_started_at = now_jst()
    state.is_paused = False
    state.paused_remaining_seconds = None
    db.commit()
    return RedirectResponse(url="/admin/status", status_code=303)


@app.post("/admin/status/skip_round")
def admin_status_skip_round(db: Session = Depends(get_db), _auth: None = Depends(require_admin)):
    today = today_jst()
    state = get_or_create_state(db, today)
    state.current_round += 1
    # スキップ後は自動的に走らせず、指導員がStartを押すまで待つ
    state.is_running = False
    state.round_started_at = None
    state.is_paused = False
    state.paused_remaining_seconds = None
    db.commit()
    return JSONResponse({"status": "ok"})


@app.post("/admin/status/pause_round", response_class=HTMLResponse)
def admin_status_pause_round(db: Session = Depends(get_db), _auth: None = Depends(require_admin)):
    today = today_jst()
    state = get_or_create_state(db, today)
    setting = db.query(SparringSettings).order_by(SparringSettings.id.desc()).first()

    if state.is_running and not state.is_paused and setting and state.round_started_at:
        mm, ss = map(int, setting.round_duration.split(":"))
        duration_seconds = mm * 60 + ss
        elapsed = (now_jst() - state.round_started_at).total_seconds()
        state.paused_remaining_seconds = max(int(duration_seconds - elapsed), 0)
        state.is_paused = True
        db.commit()

    return RedirectResponse(url="/admin/status", status_code=303)


@app.post("/admin/status/resume_round", response_class=HTMLResponse)
def admin_status_resume_round(db: Session = Depends(get_db), _auth: None = Depends(require_admin)):
    today = today_jst()
    state = get_or_create_state(db, today)
    setting = db.query(SparringSettings).order_by(SparringSettings.id.desc()).first()

    if state.is_paused and setting and state.paused_remaining_seconds is not None:
        if state.paused_remaining_seconds <= 0:
            # 時間切れの状態で「いいえ」を押して一時停止していた場合、再開は
            # 同じラウンドを0秒から動かすのではなく、そのまま次のラウンドへ進む
            state.current_round += 1
            state.is_running = True
            state.round_started_at = now_jst()
            state.is_paused = False
            state.paused_remaining_seconds = None
        else:
            mm, ss = map(int, setting.round_duration.split(":"))
            duration_seconds = mm * 60 + ss
            elapsed_before_pause = duration_seconds - state.paused_remaining_seconds
            state.round_started_at = now_jst() - timedelta(seconds=elapsed_before_pause)
            state.is_paused = False
            state.paused_remaining_seconds = None
        db.commit()

    return RedirectResponse(url="/admin/status", status_code=303)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"起動中: {get_base_url()}")
    uvicorn.run(app, host="0.0.0.0", port=port)
