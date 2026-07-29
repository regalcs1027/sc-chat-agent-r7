"""
notify.py  –  未確認の質問と、管理者による回答修正をメールで通知する

GitHub Actions から毎時実行される。Streamlit Cloud のアプリは誰も使っていないと
プロセスが止まるため、アプリ内のタイマーでは通知を取りこぼす。そのため外部から回す。

【二重送信しない仕組み】
通知した対象には DB 側に notified_at の印を付ける。
- 質問  … messages.notified_at
- 修正  … admin_rulings.notified_at
一度通知した質問は、未確認のまま残っても再通知しない（要望どおり）。

【安全策】
- 宛先は環境変数で固定。質問内容など外部入力から宛先が決まることはない
- 件名・宛先に外部由来の文字列を入れない（ヘッダーインジェクション対策）
- 1回の実行で送るのは MAX_MAILS 通まで。不具合で大量送信しないための歯止め
- 送信専用。メールの読み取りは一切しない
"""
import os
import smtplib
import sys
from email.message import EmailMessage

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import (  # noqa: E402
    create_tables,
    get_unnotified_questions, mark_questions_notified,
    get_unnotified_rulings, mark_rulings_notified,
)

APP_URL = {
    "R7": "https://sc-chat-agent-r07.streamlit.app",
    "R8": "https://sc-chat-agent-r08.streamlit.app",
}
YEAR_LABEL = {"R7": "令和7年度版", "R8": "令和8年度版"}

MAX_MAILS = 10          # 1回の実行で送る上限（暴走防止）
HEAD_CHARS = 40         # 未確認通知に載せる質問の文字数
BODY_CHARS = 400        # 修正通知に載せる本文の文字数
OLDER_THAN_MINUTES = 60  # 投稿からこの時間が経った質問を通知対象にする

_sent = 0


def _cut(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n] + "…"


def send_mail(to_addr: str, subject: str, body: str) -> None:
    """SMTPで1通送る。件名・宛先は呼び出し側が組み立てた固定文字列のみを渡すこと。"""
    global _sent
    if _sent >= MAX_MAILS:
        print(f"  送信上限 {MAX_MAILS} 通に達したため送信しません")
        return

    msg = EmailMessage()
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)

    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=30) as s:
        s.starttls()
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)
    _sent += 1
    print(f"  送信しました: {subject}")


def notify_unreviewed(app_year: str, to_addr: str) -> None:
    rows = get_unnotified_questions(app_year, OLDER_THAN_MINUTES)
    if not rows:
        print(f"[{app_year}] 未通知の質問なし")
        return

    label = YEAR_LABEL.get(app_year, app_year)
    lines = [f"未確認の質問が {len(rows)} 件あります。", ""]
    for r in rows:
        lines.append(f"・{r['created_at'][5:16]}  {r['display_name']}  {_cut(r.get('question'), HEAD_CHARS)}")
    lines += ["", "内容の確認はこちらから:", APP_URL.get(app_year, "")]

    send_mail(to_addr,
              f"【SCチャットエージェント {label}】未確認の質問が{len(rows)}件あります",
              "\n".join(lines))
    mark_questions_notified([r["answer_id"] for r in rows])


def notify_revisions(to_addr: str) -> None:
    rows = get_unnotified_rulings()
    if not rows:
        print("未通知の修正なし")
        return

    done = []
    for r in rows:
        label = YEAR_LABEL.get(r["app_year"], r["app_year"])
        body = "\n".join([
            f"{r.get('created_by_name') or '管理者'} が回答を修正しました。",
            "",
            f"【質問】\n{_cut(r.get('question_text'), BODY_CHARS)}",
            "",
            f"【修正前のAI回答】\n{_cut(r.get('original_answer'), BODY_CHARS)}",
            "",
            f"【修正後の回答】\n{_cut(r.get('corrected_answer'), BODY_CHARS)}",
            "",
            f"適用範囲: {r.get('domain_key') or '全体共通'}",
            "",
            "確認はこちらから:",
            APP_URL.get(r["app_year"], ""),
        ])
        send_mail(to_addr, f"【SCチャットエージェント {label}】回答が修正されました", body)
        done.append(r["id"])
    mark_rulings_notified(done)


def main() -> int:
    missing = [k for k in ("DATABASE_URL", "SMTP_USER", "SMTP_PASS") if not os.environ.get(k)]
    if missing:
        print(f"エラー: 環境変数が設定されていません: {missing}")
        return 1

    # 通知に使う列は Streamlit アプリの起動時にしか作られない。
    # アプリが Reboot されていないと列が無くて落ちるため、ここでも用意しておく。
    # すべて IF NOT EXISTS なので、何度実行しても既存データには影響しない。
    create_tables()

    to_q = os.environ.get("MAIL_TO_QUESTION", "")
    to_r = os.environ.get("MAIL_TO_REVISION", "")

    if to_q:
        for year in ("R7", "R8"):
            notify_unreviewed(year, to_q)
    else:
        print("MAIL_TO_QUESTION 未設定のため未確認通知はスキップ")

    if to_r:
        notify_revisions(to_r)
    else:
        print("MAIL_TO_REVISION 未設定のため修正通知はスキップ")

    print(f"合計 {_sent} 通送信しました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
