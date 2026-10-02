"""筋トレ報告ボット(GitHub Actions版)

起動後、締切まで10秒おきにSlackを確認する。
- REMIND_TIME になったら自分へDMを送り、DEADLINE_TIME のさぼり投稿を予約する
- DMのスレッドに自分が返信したら予約を取り消し、連続記録を投稿して終了
- timesに「今日は〇〇で筋トレを休みます」と投稿したら、フリーズトークンを1個使って休む
  (報告7日ごとにトークンを1個獲得)
状態はDMメッセージのmetadataに保存する(リポジトリには何も残さない)。
"""
import os
import re
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from slack_sdk import WebClient

TZ = ZoneInfo(os.environ.get("TZ_NAME", "Asia/Tokyo"))
USER_ID = os.environ["SLACK_USER_ID"]
TIMES_CHANNEL_ID = os.environ["SLACK_TIMES_CHANNEL_ID"]
REMIND_TIME = os.environ.get("REMIND_TIME", "20:00")
DEADLINE_TIME = os.environ.get("DEADLINE_TIME", "23:59")
LAZY_TEXT = os.environ.get("LAZY_TEXT", "今日は筋トレをさぼっています :zzz:")
TOKEN_EVERY = 7  # 報告が何日たまるごとにトークンを1個もらえるか
POLL_SECONDS = 10

client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])


def now():
    return datetime.now(TZ)


def at_today(hhmm):
    h, m = map(int, hhmm.split(":"))
    return now().replace(hour=h, minute=m, second=0, microsecond=0)


def meta(msg):
    m = msg.get("metadata") or {}
    return m.get("event_type"), m.get("event_payload") or {}


def load(dm):
    """DM履歴から (今日のリマインド, 報告済み日付, リマインド日付, フリーズ日付) を取り出す。"""
    oldest = (now() - timedelta(days=400)).timestamp()
    done, reminded, frozen, reminder = set(), set(), set(), None
    cursor = None
    while True:
        res = client.conversations_history(
            channel=dm, oldest=str(oldest), limit=200, cursor=cursor,
            include_all_metadata=True,
        )
        for msg in res["messages"]:
            kind, payload = meta(msg)
            if kind == "kintore_done":
                done.add(payload["date"])
            elif kind == "kintore_freeze":
                frozen.add(payload["date"])
            elif kind == "kintore_reminder":
                reminded.add(payload["date"])
                if payload["date"] == now().date().isoformat():
                    reminder = {"ts": msg["ts"], **payload}
        cursor = (res.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return reminder, done, reminded, frozen


def simulate(done, frozen, reminded):
    """履歴を日ごとにたどって、連続日数・トークン数などを計算する。

    今日が報告済み/フリーズ済みなら今日まで、そうでなければ昨日までを数える。
    """
    today = now().date()
    days = done | frozen | reminded
    s = dict(streak=0, progress=0, tokens=0, total=0, missed=0, earned=False)
    if not days:
        return s
    end = today if (today.isoformat() in done or today.isoformat() in frozen) else today - timedelta(days=1)
    d = date.fromisoformat(min(days))
    while d <= end:
        ds = d.isoformat()
        s["earned"] = False
        if ds in done:
            s["streak"] += 1
            s["total"] += 1
            s["progress"] += 1
            s["missed"] = 0
            if s["progress"] >= TOKEN_EVERY:
                s["tokens"] += 1
                s["progress"] = 0
                s["earned"] = True
        elif ds in frozen and s["tokens"] > 0:
            s["tokens"] -= 1
            s["missed"] = 0
        else:
            s["streak"] = 0
            s["progress"] = 0
            s["missed"] += 1
        d += timedelta(days=1)
    return s


def summary(s):
    text = (
        f":fire: 連続 *{s['streak']}日*(累計 {s['total']}日)\n"
        f":snowflake: フリーズトークン *{s['tokens']}個* / 次のトークンまであと *{TOKEN_EVERY - s['progress']}日*"
    )
    if s["earned"]:
        text = ":tada: 7日達成! フリーズトークンを1個獲得しました!\n" + text
    return text


def cleanup_status():
    """timesのボット投稿(連続記録/さぼり/フリーズ)のうち、最新の1件だけ残して削除する。"""
    oldest = (now() - timedelta(days=7)).timestamp()
    res = client.conversations_history(
        channel=TIMES_CHANNEL_ID, oldest=str(oldest), limit=200, include_all_metadata=True
    )
    status = sorted(
        (m for m in res["messages"] if meta(m)[0] == "kintore_status"),
        key=lambda m: float(m["ts"]),
        reverse=True,
    )
    for m in status[1:]:
        try:
            client.chat_delete(channel=TIMES_CHANNEL_ID, ts=m["ts"])
        except Exception as e:
            print("delete failed:", e)


def is_freeze_text(text):
    return "筋トレ" in text and "休" in text and not re.search(r"休(ま|み)(ない|ません)", text)


def find_freeze_requests():
    """今日、timesに自分が投稿した「筋トレを休みます」系のメッセージ(古い順)。"""
    res = client.conversations_history(
        channel=TIMES_CHANNEL_ID, oldest=str(at_today("00:00").timestamp()), limit=200
    )
    mine = [
        m for m in res["messages"]
        if m.get("user") == USER_ID and not m.get("bot_id") and is_freeze_text(m.get("text", ""))
    ]
    return sorted(mine, key=lambda m: float(m["ts"]))


def cancel_lazy(reminder):
    if not reminder:
        return
    try:
        client.chat_deleteScheduledMessage(
            channel=TIMES_CHANNEL_ID, scheduled_message_id=reminder["scheduled_id"]
        )
    except Exception as e:
        print("cancel failed:", e)


def send_reminder(dm, done, reminded, frozen):
    deadline = at_today(DEADLINE_TIME)
    s = simulate(done, frozen, reminded)
    scheduled = client.chat_scheduleMessage(
        channel=TIMES_CHANNEL_ID,
        post_at=int(deadline.timestamp()),
        text=f"{LAZY_TEXT}(さぼり {s['missed'] + 1}日目)",
        metadata={"event_type": "kintore_status", "event_payload": {"kind": "lazy"}},
    )
    today = now().date().isoformat()
    client.chat_postMessage(
        channel=dm,
        text=(
            ":muscle: 今日の筋トレ報告をこのスレッドに返信してください。\n"
            f"{DEADLINE_TIME} までに返信がないと、timesに「{LAZY_TEXT}」と投稿されます。\n"
            f"{summary(s)}\n"
            "休む場合は、timesに「今日は〇〇で筋トレを休みます」と投稿するとフリーズトークンを使えます。"
        ),
        metadata={
            "event_type": "kintore_reminder",
            "event_payload": {"date": today, "scheduled_id": scheduled["scheduled_message_id"]},
        },
    )


def has_my_reply(dm, ts):
    res = client.conversations_replies(channel=dm, ts=ts, limit=50)
    return any(m.get("user") == USER_ID and not m.get("bot_id") for m in res["messages"][1:])


def confirm(dm, reminder, done, reminded, frozen):
    cancel_lazy(reminder)
    today = now().date().isoformat()
    s = simulate(done | {today}, frozen, reminded)
    client.chat_postMessage(
        channel=dm,
        thread_ts=reminder["ts"],
        text=f":white_check_mark: 報告を確認しました。さぼり投稿は取り消しました。\n{summary(s)}",
        metadata={"event_type": "kintore_done", "event_payload": {"date": today}},
    )
    client.chat_postMessage(
        channel=TIMES_CHANNEL_ID,
        text=f":muscle: 今日も筋トレ報告しました!\n{summary(s)}",
        metadata={"event_type": "kintore_status", "event_payload": {"kind": "streak"}},
    )
    cleanup_status()


def try_freeze(dm, request, reminder, done, reminded, frozen):
    """フリーズを使えるなら使って True を返す。トークン不足なら案内して False。"""
    today = now().date().isoformat()
    if simulate(done, frozen, reminded)["tokens"] < 1:
        client.chat_postMessage(
            channel=TIMES_CHANNEL_ID,
            thread_ts=request["ts"],
            text=":no_entry: フリーズトークンがありません。7日報告するとトークンがもらえます。",
        )
        return False
    client.chat_postMessage(
        channel=dm,
        text=":snowflake: 今日はフリーズを使って休みます。",
        metadata={"event_type": "kintore_freeze", "event_payload": {"date": today}},
    )
    cancel_lazy(reminder)
    s = simulate(done, frozen | {today}, reminded)
    client.chat_postMessage(
        channel=TIMES_CHANNEL_ID,
        text=f":snowflake: 今日はフリーズを使って休みます。連続記録はキープ!\n{summary(s)}",
        metadata={"event_type": "kintore_status", "event_payload": {"kind": "freeze"}},
    )
    cleanup_status()
    return True


def main():
    dm = client.conversations_open(users=USER_ID)["channel"]["id"]
    deadline = at_today(DEADLINE_TIME)
    denied = set()
    while now() < deadline:
        reminder, done, reminded, frozen = load(dm)
        today = now().date().isoformat()
        if today in done or today in frozen:
            print("already reported or frozen today")
            return
        for request in find_freeze_requests():
            if request["ts"] in denied:
                continue
            if try_freeze(dm, request, reminder, done, reminded, frozen):
                print("frozen")
                return
            denied.add(request["ts"])
        if reminder is None:
            if now() >= at_today(REMIND_TIME) and now() < deadline - timedelta(seconds=90):
                send_reminder(dm, done, reminded, frozen)
                print("reminder sent")
        elif has_my_reply(dm, reminder["ts"]):
            confirm(dm, reminder, done, reminded, frozen)
            print("confirmed")
            return
        time.sleep(POLL_SECONDS)
    print("deadline reached")
    # 予約のさぼり投稿が出るのを待ってから、古いものを消す
    if (now() - deadline).total_seconds() < 600:
        time.sleep(60)
        cleanup_status()


if __name__ == "__main__":
    main()
