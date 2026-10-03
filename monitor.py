#!/usr/bin/env python3
"""
RUNNET「ゆずれ～る」空き枠監視（GitHub Actions用・1回実行して終了）
標準ライブラリのみ。状態は state.json に保存し、変化したときだけワークフローがコミットする。

Secrets（Settings → Secrets and variables → Actions）:
  GMAIL_APP_PASSWORD  Gmailアプリパスワード16桁
  MAIL_FROM           送信元Gmailアドレス
  MAIL_TO             宛先（カンマ区切りで複数可。未設定ならMAIL_FROM宛）

手動テスト: Actionsタブ → yuzureru-monitor → Run workflow → mode で test-mail / once を選ぶ
"""
import os
import re
import sys
import json
import smtplib
import datetime as dt
import urllib.request
from email.mime.text import MIMEText
from email.header import Header

# ===================== 設定 =====================
RACE_ID = "396339"          # 大会詳細ページURLの raceId=xxxxx の数字
RACE_NAME = "静岡マラソンエントリー"   # メール件名用

# 「ゆずれ～る実施大会一覧」で狙いの大会が載っているページのURL（複数可・上から探す）
LIST_URLS = [
    "https://runnet.jp/parts/2027/396339/entry.html#id01_m019",
]

RENOTIFY_MIN = 60           # 空きが続いている間の再通知間隔
NET_FAIL_THRESHOLD = 3      # 連続失敗が何回でエラー通知するか
ERROR_NOTIFY_HOURS = 3      # エラー通知の最短間隔
# ================================================

JST = dt.timezone(dt.timedelta(hours=9))
STATE_FILE = "state.json"
DETAIL_URL = f"https://runnet.jp/entry/runtes/user/pc/competitionDetailAction.do?raceId={RACE_ID}"
UA = "Mozilla/5.0 (personal yuzureru watcher; low frequency)"

NO_NOTIFY = {
    "balloon_yuzureru_yoko.png": "開始前",
    "balloon_yuzureru_none.png": "空きなし",
}


def now():
    return dt.datetime.now(JST)


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def minutes_since(iso):
    if not iso:
        return 10**9
    return (now() - dt.datetime.fromisoformat(iso)).total_seconds() / 60


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        charset = r.headers.get_content_charset() or "utf-8"
        return r.read().decode(charset, errors="replace")


def check_status():
    for url in LIST_URLS:
        html = fetch(url)
        m = re.search(rf"raceId={RACE_ID}\b", html)
        if not m:
            continue
        start = html.rfind("<tr", 0, m.start())
        end = html.find("</tr>", m.end())
        row = html[start:end] if start != -1 and end != -1 else html[max(0, m.start() - 3000):m.end() + 3000]
        imgs = re.findall(r"(balloon_yuzureru_[a-z0-9_]+\.png)", row)
        if not imgs:
            raise RuntimeError("大会の行は見つかったが、空き状況画像が見つからない（ページ構造変更？）")
        img = imgs[0]
        if img in NO_NOTIFY:
            return "NONE", NO_NOTIFY[img]
        return "AVAILABLE", f"空きあり表示（{img}）"
    raise RuntimeError("一覧ページに対象の大会が見つからない（ページ送りでずれた？LIST_URLSを確認）")

def send_mail(subject, body):
    pw = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
    frm = os.environ.get("MAIL_FROM", "").strip()
    to = os.environ.get("MAIL_TO", "").strip() or frm
    if not (pw and frm):
        sys.exit("Secrets の GMAIL_APP_PASSWORD / MAIL_FROM が未設定です")
    rcpts = [a.strip() for a in to.split(",") if a.strip()]
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = frm
    msg["To"] = ", ".join(rcpts)

    last_err = None
    for attempt in ("ssl465", "tls587"):
        try:
            if attempt == "ssl465":
                s = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30)
            else:
                s = smtplib.SMTP("smtp.gmail.com", 587, timeout=30)
                s.ehlo()
                s.starttls()
                s.ehlo()
            with s:
                s.login(frm, pw)
                s.sendmail(frm, rcpts, msg.as_string())
            return
        except smtplib.SMTPAuthenticationError:
            raise  # パスワード誤りは再試行しても無駄
        except Exception as e:
            print(f"送信失敗({attempt}): {e}")
            last_err = e
    raise last_err


def main():
    mode = os.environ.get("MODE", "monitor")
    if mode == "test-mail":
        send_mail("【テスト】ゆずれ～る監視", "このメールが届けば通知設定OKです。")
        print("テストメール送信")
        return
    if mode == "once":
        print(check_status())
        return

    st = load_state()
    # 月が変わると state.json が変化→コミットされ、60日無更新での自動停止を防ぐ
    st["keepalive_month"] = now().strftime("%Y-%m")

    try:
        state, info = check_status()
        st["fails"] = 0
        print(f"{now():%m/%d %H:%M} {state} / {info}")
        if state == "AVAILABLE":
            if minutes_since(st.get("last_notify")) >= RENOTIFY_MIN:
                send_mail(f"🏃【空きあり】{RACE_NAME} ゆずれ～る",
                          f"{info}\n今すぐエントリー（要ログイン）:\n{DETAIL_URL}\n\n"
                          "※枠は十数分で埋まることがあります")
                st["last_notify"] = now().isoformat(timespec="minutes")
                print("→ 通知メール送信")
        else:
            st["last_notify"] = None
        st["state"] = state
    except Exception as e:
        st["fails"] = st.get("fails", 0) + 1
        print(f"エラー({st['fails']}回連続): {e}")
        if st["fails"] >= NET_FAIL_THRESHOLD and minutes_since(st.get("last_error")) >= ERROR_NOTIFY_HOURS * 60:
            try:
                send_mail(f"⚠️ ゆずれ～る監視エラー（{RACE_NAME}）", str(e))
                st["last_error"] = now().isoformat(timespec="minutes")
            except Exception as me:
                print(f"エラー通知も失敗: {me}")

    save_state(st)


if __name__ == "__main__":
    main()
